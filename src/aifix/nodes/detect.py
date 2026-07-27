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
from ..graph import AifixState
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
    )
    with json_output():
        outcome = await consume(loop.run(build_prompt(failure, candidates)))

    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    return {
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
    }
