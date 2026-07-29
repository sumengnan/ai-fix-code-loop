from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from .config import AifixConfig

# `abort_kind` 的一个取值：baseline 里文件级收集错误占比过高，这批测量不可信。
# 判据与消息在 nodes/baseline.collection_error_abort，常量放在这里是因为
# **abort_kind 的契约由这个模块定义**，而读它的有三方（报告、CLI 退出码、
# 评测的成绩/故障分类）—— 常量若跟着判据走，nodes/report 就得为了一个字符串
# 去 import 整个 baseline 模块（连带 harness 沙箱）。
COLLECTION_ABORT_KIND = "collect"

# `abort_kind` 的另一个取值：配置的模型端点连不上。与 collect 同类 —— 说的都是
# 「跑这次 run 的环境不对」，不是「模型没修好」。分类必须对：评测据此把它划进
# **评测故障**而不是成绩，否则模型要替我们的网络背锅。
MODEL_ABORT_KIND = "model"


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
    artifact_dir: str

    baseline_ids: list[str]
    queue: list[str]
    current: str | None
    attempt: int

    diagnosis: dict[str, Any] | None
    # 本轮诊断是否有**源码**栈帧可依。纯断言失败的 traceback 里只有测试文件
    # 那一帧，此时 suspect_file 是模型按包名猜的，`files_outside_suspect`
    # 据此决定要不要发声（见 signals.analyze）。只有 detect_node 会写它。
    suspect_anchored: bool
    verdict: str | None

    touched: list[str]
    guard_hits: list[str]
    diff_lines: int
    abort_reason: str | None
    flaky_filtered: list[str]
    confirmed_regressions: list[str]
    # 补丁合理性的静态信号。**不参与任何判定**，只由 verify 算出、由报告展示
    # 给人看。每个元素是 `{"test_id": ..., **PatchSignals 的字段}`。
    # 是**列表**不是单个 dict：核心循环对 baseline 里每个 failure 各跑一轮
    # verify，而报告在整个 run 结束后才渲染 —— 单个 dict 会被后一轮整个替换
    # 掉，多 failure 时报告只剩最后一个补丁的信号。带 test_id 是为了让人分得
    # 清是哪一次改动删的符号。只有判 BETTER（补丁真的交付了）才追加。
    signals: list[dict[str, Any]]
    consecutive_failures: int
    failure_token_budget: int | None
    # 本轮 failure 分到的美元额度。**只有 cli.run_once 会填**：build_graph()
    # 那条路径没有 RunBudget，整条美元闸不存在，这个字段一直是 None。
    # None 与 0.0 语义不同——None 是「不设美元闸」，0.0 是「额度已扣光，
    # 一次调用都不许发起」，见 fix_node 里的 is None 判定。
    failure_usd_budget: float | None
    cost_capped: bool

    spent_usd: float
    spent_tokens: int

    results: list[dict[str, Any]]
    abort: str | None
    # 中止的**种类**（budget.exhaustion 的取值：tokens / usd / wall；运行
    # 崩溃为 crash；baseline 全是收集错误为 collect；熔断为 None）。abort 是
    # 给人看的消息，种类是给程序判的：评测要据此区分「模型没在预算内修好」
    # （成绩）与「评测调度器的墙钟耗尽 / 这台机器缺依赖」（故障）。
    abort_kind: str | None
    report_md: str

    # baseline 解析出的 Failure 对象，按 test_id 索引。
    # 下划线前缀表示它不参与路由判断，只作为 detect / verify 的数据源。
    _failures: dict[str, Any]
    # baseline 跑出结果的用例总数，只供进度显示（见 progress.py）
    _ran: int
    # 当前 run 的 RunTrace。同样不参与路由，只是各节点写观测数据的出口。
    _trace: Any
    # 进度回调（见 progress.py），侧信道，理由同 _trace
    _progress: Any


def new_state(repo: Path, config: AifixConfig, run_id: str) -> AifixState:
    return AifixState(
        run_id=run_id, repo=str(repo), config=config,
        adapter_name="", worktree_path="", branch="", artifact_dir="",
        baseline_ids=[], queue=[], current=None, attempt=0,
        diagnosis=None, verdict=None,
        touched=[], guard_hits=[], diff_lines=0, abort_reason=None,
        flaky_filtered=[], confirmed_regressions=[], signals=[],
        consecutive_failures=0,
        failure_token_budget=None,
        failure_usd_budget=None, cost_capped=False,
        spent_usd=0.0, spent_tokens=0,
        results=[], abort=None, abort_kind=None,
    )


class _NullTrace:
    """trace 缺席时的空实现 —— 单测直接调节点时不必构造 trace。"""

    def fact(self, *a: Any, **k: Any) -> None: ...

    def record_events(self, *a: Any, **k: Any) -> None: ...


def trace_of(state: AifixState):
    """取当前 run 的 trace；未接线时返回一个吞掉所有调用的空实现。"""
    t = state.get("_trace")
    return t if t is not None else _NullTrace()


def progress_of(state: AifixState):
    """取当前 run 的进度回调；未接线时返回哑实现。

    与 trace_of 同一套约定（侧信道放在 state 里），理由也一样：进度是
    **横切**的，让每个节点的签名各加一个参数，等于每加一处出声点就要改一遍
    调用链。哑实现是默认值 —— eval 并行跑几十个 run，默认出声会串成一团。
    """
    from .progress import NullProgress
    p = state.get("_progress")
    return p if p is not None else NullProgress()


def check_circuit_breaker(state: AifixState) -> str | None:
    """连续失败达到阈值就中止整个 run，返回中止原因；未达阈值返回 None。

    连着几个 failure 一个都没修好，大概率不是「这些 bug 恰好都难」，
    而是环境坏了、prompt 崩了、或今天这个模型不行。继续跑只是匀速烧钱。
    比预算上限更早生效，也更有信息量 —— 它把「钱花完了」变成
    「出问题了，去看 trace」。
    """
    limit = state["config"].consecutive_failure_limit
    n = state.get("consecutive_failures", 0)
    if n >= limit:
        return f"连续 {n} 个 failure 均未修复，疑似系统性问题，已中止"
    return None


def route_after_baseline(state: AifixState) -> str:
    """全绿或已中止 → 直接出报告；否则取第一个 failure 开始处理。"""
    if state.get("abort") or not state["queue"]:
        return "report"
    return "detect"


def route_after_verify(state: AifixState) -> str:
    """current 仍在 → 同一个 failure 重试；已清空 → 取下一个或收尾。"""
    if state.get("abort") or check_circuit_breaker(state):
        return "report"
    if state["current"] is not None:
        return "detect"
    return "detect" if state["queue"] else "report"


def checkpointer_for(config: AifixConfig, artifact_dir: Path):
    """按配置建 LangGraph 的 SqliteSaver；未开启返回 None。

    SqliteSaver 在独立包 langgraph-checkpoint-sqlite 里，
    langgraph 本体只依赖抽象基座 langgraph-checkpoint。
    """
    if not config.enable_checkpoint:
        return None
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    d = Path(artifact_dir)
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / "checkpoint.sqlite"), check_same_thread=False)
    return SqliteSaver(conn)


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
