from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import FailureSet, Verdict
from ..delivery import Worktree
from ..graph import AifixState, trace_of
from ..verify import compare
from .baseline import adapter_for, run_full_suite, run_scoped


def _worktree(state: AifixState) -> Worktree:
    """指向已存在的 worktree —— **不进入上下文管理器**。

    worktree 由 cli.run_once 建立并负责移除；这里只借用 commit / rollback
    这两个纯路径操作。若在此 `with`，退出时会把还在用的 worktree 删掉。
    """
    return Worktree(Path(state["repo"]), run_id=state["run_id"])


async def filter_flaky(baseline: FailureSet, current: FailureSet,
                       rerun) -> tuple[set[str], set[str]]:
    """把「新增失败」拆成确认回归与抖动两部分。

    只在出现新失败时触发重跑，且只重跑那几个用例 —— 成本近似为零，
    却能挡掉绝大部分因抖动导致的误回滚（把一个本来正确的补丁滚掉，
    是这个系统最昂贵的错误）。

    rerun：async callable，接收 test_id 集合，返回重跑后的 FailureSet。
    返回 (确认回归, 判为抖动)。
    """
    new = current.ids - baseline.ids
    if not new:
        return set(), set()
    again = await rerun(sorted(new))
    confirmed = new & again.ids
    return confirmed, new - confirmed


async def verify_node(state: AifixState) -> dict[str, Any]:
    """跑全量、过滤抖动、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapter = adapter_for(state["adapter_name"])
    worktree_path = Path(state["worktree_path"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    current = await run_full_suite(worktree_path, adapter)

    async def _rerun(ids: list[str]) -> FailureSet:
        return await run_scoped(worktree_path, adapter, ids)

    confirmed, flaky = await filter_flaky(baseline, current, _rerun)
    # 抖动的用例从当前结果里剔除，避免它们把判定拖成 WORSE
    effective = FailureSet({k: v for k, v in current.failures.items()
                            if k not in flaky})
    verdict = compare(baseline, effective, target)

    trace = trace_of(state)
    trace.fact("verdict", verdict.value)
    if flaky:
        trace.fact("flaky_filtered", sorted(flaky))
    if confirmed:
        trace.fact("confirmed_regressions", sorted(confirmed))
    if verdict is not Verdict.BETTER:
        trace.fact("rollback", True)

    results = list(state["results"])
    common = {"flaky_filtered": sorted(flaky),
              "confirmed_regressions": sorted(confirmed)}

    if verdict is Verdict.BETTER:
        wt.commit(f"fix: {target}", paths=state.get("touched") or [])
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"], "abort_reason": None})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": 0, **common}

    wt.rollback()
    if state["attempt"] >= cfg.max_attempts:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"],
                        "abort_reason": state.get("abort_reason") or "max_attempts"})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": state.get("consecutive_failures", 0) + 1,
                **common}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None, **common}
