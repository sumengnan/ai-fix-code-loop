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
        spent_usd=0.0, spent_tokens=0,
        results=[], abort=None,
    )
