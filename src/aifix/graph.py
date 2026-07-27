from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from .config import AifixConfig


class AifixState(TypedDict, total=False):
    """LangGraph 的宏观状态：跨 failure 的进度。

    单次 AgentLoop 内部的微观状态由框架自己管，两层互不知道对方。
    """
    run_id: str
    repo: str
    config: AifixConfig

    adapter_name: str
    worktree_path: str
    branch: str

    baseline_ids: list[str]
    queue: list[str]
    current: str | None
    attempt: int

    diagnosis: dict[str, Any] | None
    verdict: str | None

    touched: list[str]
    guard_hits: list[str]
    diff_lines: int
    abort_reason: str | None

    spent_usd: float
    spent_tokens: int

    results: list[dict[str, Any]]
    abort: str | None
    report_md: str

    # baseline 解析出的 Failure 对象，按 test_id 索引。
    # 下划线前缀表示它不参与路由判断，只作为 detect / verify 的数据源。
    _failures: dict[str, Any]


def new_state(repo: Path, config: AifixConfig, run_id: str) -> AifixState:
    return AifixState(
        run_id=run_id, repo=str(repo), config=config,
        adapter_name="", worktree_path="", branch="",
        baseline_ids=[], queue=[], current=None, attempt=0,
        diagnosis=None, verdict=None,
        touched=[], guard_hits=[], diff_lines=0, abort_reason=None,
        spent_usd=0.0, spent_tokens=0,
        results=[], abort=None,
    )


def route_after_baseline(state: AifixState) -> str:
    """全绿或已中止 → 直接出报告；否则取第一个 failure 开始处理。"""
    if state.get("abort") or not state["queue"]:
        return "report"
    return "detect"


def route_after_verify(state: AifixState) -> str:
    """current 仍在 → 同一个 failure 重试；已清空 → 取下一个或收尾。"""
    if state.get("abort"):
        return "report"
    if state["current"] is not None:
        return "detect"
    return "detect" if state["queue"] else "report"


def build_graph(checkpointer: Any = None):
    """装配 LangGraph。节点是 trace 的单位，也是 checkpoint 的边界。"""
    from langgraph.graph import END, StateGraph

    from .nodes.baseline import baseline_node
    from .nodes.detect import detect_node
    from .nodes.fix import fix_node
    from .nodes.preflight import preflight_node
    from .nodes.report import report_node
    from .nodes.verify import verify_node

    def _take_next(state: AifixState) -> dict[str, Any]:
        if state["current"] is not None:
            return {}
        queue = list(state["queue"])
        return {"current": queue.pop(0), "queue": queue, "attempt": 1}

    g = StateGraph(AifixState)
    g.add_node("preflight", preflight_node)
    g.add_node("baseline", baseline_node)
    g.add_node("take_next", _take_next)
    g.add_node("detect", detect_node)
    g.add_node("fix", fix_node)
    g.add_node("verify", verify_node)
    g.add_node("report", report_node)

    g.set_entry_point("preflight")
    g.add_conditional_edges(
        "preflight",
        lambda s: "report" if s.get("abort") else "baseline",
        {"report": "report", "baseline": "baseline"})
    g.add_conditional_edges(
        "baseline", route_after_baseline,
        {"report": "report", "detect": "take_next"})
    g.add_edge("take_next", "detect")
    g.add_edge("detect", "fix")
    g.add_edge("fix", "verify")
    g.add_conditional_edges(
        "verify", route_after_verify,
        {"report": "report", "detect": "take_next"})
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
