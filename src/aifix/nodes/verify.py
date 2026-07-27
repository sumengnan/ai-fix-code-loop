from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import FailureSet, Verdict
from ..delivery import Worktree
from ..graph import AifixState
from ..verify import compare
from .baseline import adapter_for, run_full_suite


def _worktree(state: AifixState) -> Worktree:
    """指向已存在的 worktree —— **不进入上下文管理器**。

    worktree 由 cli.run_once 建立并负责移除；这里只借用 commit / rollback
    这两个纯路径操作。若在此 `with`，退出时会把还在用的 worktree 删掉。
    """
    return Worktree(Path(state["repo"]), run_id=state["run_id"])


async def verify_node(state: AifixState) -> dict[str, Any]:
    """跑全量、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapter = adapter_for(state["adapter_name"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    current = await run_full_suite(Path(state["worktree_path"]), adapter)
    verdict = compare(baseline, current, target)

    results = list(state["results"])
    if verdict is Verdict.BETTER:
        wt.commit(f"fix: {target}")
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"], "abort_reason": None})
        return {"verdict": verdict.value, "current": None,
                "attempt": 0, "results": results, "diagnosis": None}

    wt.rollback()
    if state["attempt"] >= cfg.max_attempts:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"],
                        "abort_reason": "max_attempts"})
        return {"verdict": verdict.value, "current": None,
                "attempt": 0, "results": results, "diagnosis": None}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None}
