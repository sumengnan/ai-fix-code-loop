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
from .baseline import adapter_for


async def detect_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """无工具、单步、强制 JSON。解析失败降级为 diagnosis=None。"""
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_for(state["adapter_name"])
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

    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    trace = trace_of(state)
    trace.record_events(outcome.events)
    if diagnosis is not None:
        # 模型点名的文件是否在 locate_source 的候选里 —— 评测直接取这条，
        # 不必为「定位准确率」单独埋点。
        hit = any(c.path == diagnosis.suspect_file for c in candidates)
        trace.fact("locate_hit", hit)
        trace.fact("suspect_file", diagnosis.suspect_file)
    else:
        trace.fact("locate_hit", False)
        trace.fact("diagnosis_parse_failed", True)
    return {
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
    }
