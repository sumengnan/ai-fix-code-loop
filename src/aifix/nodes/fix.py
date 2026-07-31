from __future__ import annotations

from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.sandbox.local import LocalSandbox
from harness.types import Message, Role

from ..agents.detector import Diagnosis
from ..agents.fixer import (SYSTEM_PROMPT, build_initial_messages,
                            build_registry, locate_hint)
from ..agents.runner import consume
from ..graph import AifixState, progress_of, trace_of
from ..progress import StepReporter
from ..violations import count_violations
from .baseline import adapter_from_state

_EMPTY_FEEDBACK = (
    "你没有对任何文件做出修改。只说「已修复」是无效的 —— "
    "请先用 read_file 确认文件当前的真实内容，再用 apply_patch 提交具体改动。")

# 改动落在被 .gitignore 盖住的路径上时**不能**发上面那句：模型确实改了文件，
# 说它「没有对任何文件做出修改」是一句假话，而模型照这句话去做只会再改一次
# 同样的东西，一路重试到 giveup。说实话它才有机会换个地方。
_IGNORED_FEEDBACK = (
    "你的改动落在被 .gitignore 忽略的路径上（{paths}），这些文件交付不了 —— "
    "交付分支只收 git 跟踪得到的文件，被忽略的路径 `git add` 会直接拒绝。"
    "请把修复写到不被忽略的路径上，或者改已有的源文件。")

_HUGE_FEEDBACK = (
    "你的改动范围过大（{lines} 行，上限 {limit} 行），疑似整文件重写。"
    "改动已被回滚。请只改必要的那几行，用最小的 diff 修复问题。")


def _sum_numstat(stdout: str) -> int:
    """`git ... --numstat` 的前两列求和。非数字（二进制文件是 `-`）跳过。"""
    total = 0
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    return total


async def _diff_lines(sandbox: LocalSandbox, touched: set[str]) -> int:
    """工作区当前改动的行数（+/- 行），未跟踪的新文件也算。

    `git diff` 看不见未跟踪文件，而「只新建一个文件」是完全合法的修复：
    算出 0 会被判成 empty_diff，带着「你没有对任何文件做出修改」的反馈一路
    重试到 giveup —— 模型每一轮都做对了，额度却烧光，trace 里还留下一个与
    事实不符的 diff_lines: 0。

    只统计 `touched` 里的路径。worktree 里跑过测试会留下一堆未跟踪产物
    （__pycache__、覆盖率文件、日志），把它们算进来等于让 empty_diff 这道
    守卫从此永不触发。touched 是 ApplyPatchTool 的记账，是 agent 唯一的修改
    手段 —— 与 Worktree.commit 「绝不用 git add -A」是同一条理由、同一份名单。

    `--exclude-standard` 的代价：被 .gitignore 盖住的新文件恒计 0 行。这一条
    是**有意**保留的 —— 那个文件交付不了（`git add` 对被忽略的路径直接以 1
    退出，实测），算成有效改动只会把问题推到交付那一步再炸，而那一步能给模型
    的反馈只剩一次 run 的结论。代价必须由文案兜住：见 `_ignored_paths`。
    """
    total = _sum_numstat(
        (await sandbox.exec(["git", "diff", "--numstat"], 30.0)).stdout)
    if not touched:
        # 空 pathspec 会让 ls-files 列出**全部**未跟踪文件，正是上面要避开的
        return total
    listed = await sandbox.exec(
        ["git", "ls-files", "--others", "--exclude-standard", "--",
         *sorted(touched)], 30.0)
    for rel in listed.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        # 与 /dev/null 比，行数口径和上面那次 numstat 完全一致；--no-index
        # 有差异时以 1 退出，这里只读它的输出，退出码本就不是错误
        added = await sandbox.exec(
            ["git", "diff", "--numstat", "--no-index", "--", "/dev/null", rel],
            30.0)
        total += _sum_numstat(added.stdout)
    return total


async def _ignored_paths(sandbox: LocalSandbox, touched: set[str]) -> list[str]:
    """`touched` 里被 .gitignore 盖住的那些路径。

    `--ignored` 与 `--exclude-standard` 一起用才是「只列被忽略的」——
    单给 `--ignored` 时 git 会连普通的未跟踪文件一起列出来，那样任何一次
    新建文件都会被说成「落在被忽略的路径上」。
    """
    if not touched:
        return []
    listed = await sandbox.exec(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard",
         "--", *sorted(touched)], 30.0)
    return [ln.strip() for ln in listed.stdout.splitlines() if ln.strip()]


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
    adapter = adapter_from_state(state)
    raw = state.get("diagnosis")
    diagnosis = Diagnosis.model_validate(raw) if raw else None

    # 优先用本轮分配到的额度；未分配（如单测直接调用）时退回全局剩余。
    # 当前 RunBudget.for_failure 有 FLOOR_TOKENS 下限，永远取不到 0，`or` 逻
    # 辑无害。改成 is None 判定是为了不给后人留一个「看起来能用 `or`」的坏样板
    # —— 同类的美元闸已改成严格的 is None 判定（见下方），形状要一致。
    failure_token = state.get("failure_token_budget")
    remaining = (max(cfg.budget_tokens - state["spent_tokens"], 10_000)
                 if failure_token is None else failure_token)
    # 本轮 failure 分到的美元额度。**只有 None**（未分配）才表示不设美元闸、
    # 退回 token 闸；0.0 表示「额度已经扣光」，是一个要拦死的真实取值。
    # 必须用 is None 判，不能写 `state.get(...) or None`：`0.0 or None` 求值
    # 成 None，于是「额度已被扣成 0」会被读成「不设闸」—— 恰好把闸最该拦住
    # 的那一刻变成完全不拦。额度是按「扣掉 detect 的花费之后的剩余」算的，
    # 0.0 是常见值而不是理论边界。
    usd_alloc = state.get("failure_usd_budget")
    cost_capped = False
    touched: set[str] = set()
    guard_hits: list[str] = []
    abort_reason: str | None = None
    tokens = 0
    cost = 0.0
    lines = 0

    trace = trace_of(state)
    prog = progress_of(state)
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
        # 诊断指错文件时补上真路径。放在 sandbox.start() 之后：它要问 git
        # 仓库里到底有哪些文件，而这是唯一有资格回答的人。
        messages = build_initial_messages(
            failure, diagnosis,
            locate=await locate_hint(sandbox, adapter, diagnosis))

        last_guard: str | None = None
        guard_repeats = 0

        # 心跳：模型每调一次工具报两条 —— 开始时报「在干什么」，结束时报
        # 「成了还是砸了、为什么砸」。fix 默认最多 25 步，是一次 run 里最长的
        # 一段，不报的话最需要进度的那几分钟仍然是空屏。
        #
        # 一个 reporter 跨守卫重试复用：步号在**整个 failure 内累加**（守卫
        # 重试是同一次修复尝试的延续，这也是它不计入 attempt 的理由），每轮
        # 从 1 重数会让屏幕上出现两段「第 1 步」，读起来像是重新开始了。
        reporter = StepReporter(prog)
        for _ in range(cfg.fix_guard_retries + 1):
            # 额度是**整个 failure** 的，不是每轮的：守卫重试是同一次修复
            # 尝试的延续，各轮分别给一份额度等于把上限悄悄放大数倍。
            round_cap = None if usd_alloc is None else usd_alloc - cost
            if round_cap is not None and round_cap <= 0:
                cost_capped = True
                break
            outcome = await consume(loop.run(messages=list(messages)),
                                    cost_cap=round_cap,
                                    on_tool=reporter.started,
                                    on_tool_done=reporter.finished)
            tokens += outcome.tokens
            cost += outcome.cost_usd
            if outcome.cost_capped:
                cost_capped = True
            # 每一轮都记：守卫重试时，模型「一字未改」的那一轮恰恰
            # 是最该复盘的，只记最后一轮等于把它丢了。
            trace.record_events(outcome.events)
            for kind, n in count_violations(outcome.events).items():
                for _ in range(n):
                    trace.fact("violation", kind)
            lines = await _diff_lines(sandbox, touched)

            if lines == 0:
                # 守卫种类仍是 empty_diff（可交付的改动确实是零，报告与 stats
                # 的口径不变），但反馈得说实话：模型改了文件，只是改到了一个
                # 交付不到的地方。事实另记一条，复盘时这两种零分得开。
                kind = "empty_diff"
                ignored = await _ignored_paths(sandbox, touched)
                if ignored:
                    trace.fact("ignored_paths", ignored)
                    feedback = _IGNORED_FEEDBACK.format(
                        paths="、".join(ignored))
                else:
                    feedback = _EMPTY_FEEDBACK
            elif lines > cfg.max_diff_lines:
                kind = "huge_diff"
                feedback = _HUGE_FEEDBACK.format(
                    lines=lines, limit=cfg.max_diff_lines)
                await _rollback(sandbox)
                touched.clear()
                # 已回滚，工作区确实没有改动了。不清零的话，守卫用尽时记进
                # trace 的会是回滚前的陈旧值 —— 观测数据撒谎比没有观测更糟。
                lines = 0
            else:
                abort_reason = None
                break

            guard_hits.append(kind)
            abort_reason = kind
            guard_repeats = guard_repeats + 1 if kind == last_guard else 1
            last_guard = kind
            if guard_repeats >= cfg.guard_giveup_limit:
                # 同一堵墙撞够了。把「钱花完了」变成「出问题了，去看 trace」——
                # 后者信息量大得多，省下的额度还能流给真有希望的 failure。
                abort_reason = f"{kind}_giveup"
                trace.fact("guard_giveup", kind)
                break

            if cost_capped:
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
        "cost_capped": cost_capped,
    }
