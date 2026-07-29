from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient, json_output
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.tools.base import ToolRegistry

from ..agents.detector import SYSTEM_PROMPT, build_prompt, parse_diagnosis
from ..agents.runner import consume
from ..graph import AifixState, trace_of
from ..signals import under_dirs
from .baseline import adapter_from_state


async def detect_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """无工具、单步、强制 JSON。解析失败降级为 diagnosis=None。"""
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_from_state(state)
    candidates = adapter.locate_source(failure, Path(state["worktree_path"]))

    loop = AgentLoop(
        client=client or OpenAICompatibleClient(cfg.detector),
        registry=ToolRegistry(),                       # 空：模型必然一步出文本
        context=ContextManager(SYSTEM_PROMPT),
        max_steps=1,
        budget=BudgetTracker(max_tokens=cfg.detector_max_tokens),
        model_name=cfg.detector.model,
        price_map=cfg.price_map,
    )
    with json_output():
        outcome = await consume(loop.run(build_prompt(failure, candidates)))

    # 候选里有没有**非测试**文件，决定了 suspect_file 是推断还是猜测。
    # 纯断言失败的 traceback 里只有测试文件那一帧，此时模型只能按包名猜路径，
    # 猜错是常态 —— 下游的 files_outside_suspect 据此决定要不要发声。
    anchored = any(not under_dirs(c.path, adapter.test_dirs())
                   for c in candidates)

    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    trace = trace_of(state)
    trace.record_events(outcome.events)
    if not anchored:
        # 落成事实而不是只在信号里体现：复盘时要能一眼看出这次诊断是无锚
        # 猜测，跨 run 统计也才分得清「模型定位差」与「压根没东西可定位」。
        trace.fact("suspect_unanchored", True)
    if diagnosis is not None:
        # 模型点名的文件是否落在 traceback 指出的候选里。
        # 这不是规格 §9 的 locate_hit —— 那个对 ground truth 判定，由评测
        # 计算。两者是不同的集合：异常常在下游抛出而缺陷在上游。共用一个
        # 名字会让评测悄悄量成「模型有没有照抄 traceback」。
        hit = any(c.path == diagnosis.suspect_file for c in candidates)
        trace.fact("suspect_in_traceback", hit)
        trace.fact("suspect_file", diagnosis.suspect_file)
    else:
        trace.fact("suspect_in_traceback", False)
        trace.fact("diagnosis_parse_failed", True)
    return {
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "suspect_anchored": anchored,
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
    }
