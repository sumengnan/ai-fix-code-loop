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
from ..snippet import around
from .baseline import adapter_from_state

# 喂几段源码。三段各二十来行 ≈ 一屏半，再多就把 traceback 挤到模型注意力的
# 边缘了 —— 而 traceback 仍然是最强的那条证据。
_SNIPPET_TOP = 3


async def detect_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """无工具、单步、强制 JSON。解析失败降级为 diagnosis=None。"""
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_from_state(state)
    worktree = Path(state["worktree_path"])
    candidates = adapter.locate_source(failure, worktree)
    # 把前几个候选的**真实源码**读出来一起喂进去。零 LLM、不多花一个回合。
    # 在这之前 Detector 判断「根本原因是什么」时压根没见过那段代码 ——
    # 详见 snippet.around 的模块说明。
    #
    # 只给前三个：候选列表可以很长，而三段各二十来行已经把单步调用的上下文
    # 用得差不多了。排序本身是确定性的（最深的栈帧在前），截断截掉的是最不
    # 可疑的那几个。
    snippets: dict[int, str] = {}
    for i, c in enumerate(candidates[:_SNIPPET_TOP]):
        body = around(worktree, c.path, c.line)
        if body is not None:
            snippets[i] = body

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
        outcome = await consume(loop.run(
            build_prompt(failure, candidates, snippets)))

    # 候选里有没有**非测试**文件，决定了 suspect_file 是推断还是猜测。
    # 一个源码候选都没有时模型只能按包名猜路径，猜错是常态 —— 下游的
    # files_outside_suspect 据此决定要不要发声。
    sources = [c for c in candidates if not adapter.is_test_path(c.path)]
    anchored = bool(sources)
    # 锚点**种类**要与「有没有锚点」分开记。两种强度不同：traceback 是
    # 「失败真的穿过这里」，import 只是「测试用到了这个模块」。合成一个
    # 布尔值的话，跨 run 回答不了「退到 import 之后定位准确率动没动」——
    # 而那是引入 import 退路时唯一要回答的问题。
    anchor = sources[0].origin if sources else None

    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    trace = trace_of(state)
    trace.record_events(outcome.events, outcome.event_times)
    if anchor is not None:
        trace.fact("suspect_anchor", anchor)
    else:
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
