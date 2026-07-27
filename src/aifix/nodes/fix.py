from __future__ import annotations

from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.sandbox.local import LocalSandbox
from harness.types import Message, Role

from ..agents.detector import Diagnosis
from ..agents.fixer import SYSTEM_PROMPT, build_initial_messages, build_registry
from ..agents.runner import consume
from ..graph import AifixState, trace_of
from .baseline import adapter_for

_EMPTY_FEEDBACK = (
    "你没有对任何文件做出修改。只说「已修复」是无效的 —— "
    "请先用 read_file 确认文件当前的真实内容，再用 apply_patch 提交具体改动。")

_HUGE_FEEDBACK = (
    "你的改动范围过大（{lines} 行，上限 {limit} 行），疑似整文件重写。"
    "改动已被回滚。请只改必要的那几行，用最小的 diff 修复问题。")


async def _diff_lines(sandbox: LocalSandbox) -> int:
    """工作区当前改动的行数（+/- 行）。"""
    res = await sandbox.exec(["git", "diff", "--numstat"], 30.0)
    total = 0
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    return total


async def _rollback(sandbox: LocalSandbox) -> None:
    await sandbox.exec(["git", "checkout", "--", "."], 60.0)
    await sandbox.exec(["git", "clean", "-fd"], 60.0)


async def fix_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """跑 Fixer，并在其结束后检查改动是否合理。

    两条守卫都以「带反馈重试」的方式处理，而不是直接失败：模型拿到
    具体的问题描述后，下一次通常就对了。守卫重试不计入 attempt ——
    attempt 衡量的是「修复尝试」，而这里连一次有效尝试都还没产生。
    """
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_for(state["adapter_name"])
    raw = state.get("diagnosis")
    diagnosis = Diagnosis.model_validate(raw) if raw else None

    # 优先用本轮分配到的额度；未分配（如单测直接调用）时退回全局剩余
    remaining = state.get("failure_token_budget") or max(
        cfg.budget_tokens - state["spent_tokens"], 10_000)
    touched: set[str] = set()
    guard_hits: list[str] = []
    abort_reason: str | None = None
    tokens = 0
    cost = 0.0
    lines = 0

    trace = trace_of(state)
    sandbox = LocalSandbox(workspace=state["worktree_path"])
    await sandbox.start()
    try:
        loop = AgentLoop(
            client=client or OpenAICompatibleClient(cfg.fixer),
            registry=build_registry(sandbox, adapter,
                                    known_ids=set(state["baseline_ids"]),
                                    touched=touched),
            context=ContextManager(SYSTEM_PROMPT),
            max_steps=cfg.fixer_max_steps,
            budget=BudgetTracker(max_tokens=remaining,
                                 max_wall_seconds=cfg.budget_wall_seconds),
            loop_detect_window=cfg.loop_detect_window,
            tool_result_max_chars=cfg.tool_result_max_chars,
            model_name=cfg.fixer.model,
            price_map=cfg.price_map,
        )
        messages = build_initial_messages(failure, diagnosis)

        for _ in range(cfg.fix_guard_retries + 1):
            outcome = await consume(loop.run(messages=list(messages)))
            tokens += outcome.tokens
            cost += outcome.cost_usd
            # 每一轮都记：守卫重试时，模型「一字未改」的那一轮恰恰
            # 是最该复盘的，只记最后一轮等于把它丢了。
            trace.record_events(outcome.events)
            lines = await _diff_lines(sandbox)

            if lines == 0:
                guard_hits.append("empty_diff")
                abort_reason = "empty_diff"
                feedback = _EMPTY_FEEDBACK
            elif lines > cfg.max_diff_lines:
                guard_hits.append("huge_diff")
                abort_reason = "huge_diff"
                feedback = _HUGE_FEEDBACK.format(
                    lines=lines, limit=cfg.max_diff_lines)
                await _rollback(sandbox)
                touched.clear()
                # 已回滚，工作区确实没有改动了。不清零的话，守卫用尽时
                # 记进 trace 的会是回滚前的陈旧值 —— 观测数据撒谎比没有
                # 观测更糟。
                lines = 0
            else:
                abort_reason = None
                break

            messages = messages + [
                Message(role=Role.ASSISTANT, content=outcome.text or "（无输出）"),
                Message(role=Role.USER, content=feedback),
            ]
    finally:
        await sandbox.close()

    trace.fact("diff_lines", lines)
    trace.fact("touched", sorted(touched))
    for hit in guard_hits:
        trace.fact("guard_hit", hit)

    return {
        "spent_tokens": state["spent_tokens"] + tokens,
        "spent_usd": state["spent_usd"] + cost,
        "touched": sorted(touched),
        "guard_hits": guard_hits,
        "diff_lines": lines,
        "abort_reason": abort_reason,
        "abort": None,      # AgentLoop 的错误不中止整个 run，交给 verify 判定
    }
