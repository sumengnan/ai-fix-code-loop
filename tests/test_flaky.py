from aifix.adapters.base import Failure, FailureSet
from aifix.nodes.verify import filter_flaky


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


async def test_no_new_failures_skips_rerun():
    """没有回归嫌疑就不重跑 —— 这是成本控制的关键。"""
    calls = []

    async def _rerun(ids):
        calls.append(ids)
        return _fs()

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a"), rerun=_rerun)
    assert confirmed == set()
    assert flaky == set()
    assert calls == [], "无新失败时不该触发重跑"


async def test_rerun_green_marks_flaky():
    """重跑就绿 → 判为抖动，不算回归。"""
    async def _rerun(ids):
        return _fs()          # 重跑全绿

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b"), rerun=_rerun)
    assert confirmed == set()
    assert flaky == {"b"}


async def test_rerun_still_red_confirms_regression():
    async def _rerun(ids):
        return _fs("b")       # 重跑还是红

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b"), rerun=_rerun)
    assert confirmed == {"b"}
    assert flaky == set()


async def test_only_new_failures_are_rerun():
    """只重跑新增的那几个，不是全量 —— 成本近似为零。"""
    seen = []

    async def _rerun(ids):
        seen.append(set(ids))
        return _fs("c")

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a", "b", "c"), current=_fs("a", "b", "c"), rerun=_rerun)
    assert seen == [], "全是老失败，不该重跑"

    seen.clear()
    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b", "c"), rerun=_rerun)
    assert seen == [{"b", "c"}], "只重跑新增的两个，不碰 a"
    assert confirmed == {"c"}
    assert flaky == {"b"}


async def test_better_without_changes_is_not_a_fix(buggy_repo, monkeypatch):
    """一个字节都没改却判 BETTER —— 那是 baseline 抖动，不是修复。

    空 diff 守卫不阻断流程，run 仍会走到 verify。若目标用例此时恰好
    抖成绿的，旧逻辑会：判 BETTER → commit(paths=[]) 空操作 →
    报告写「已修复」→ worktree 被删。系统宣称修好了一个它没碰过的 bug，
    这正好击穿「只有 verify 有资格说修好了」这条核心主张。
    """
    from aifix.config import AifixConfig
    from aifix.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes import verify as verify_mod
    from aifix.nodes.preflight import preflight_node

    tid = "tests/test_calc.py::test_add"

    async def _all_green(*a, **k):
        return _fs()

    monkeypatch.setattr(verify_mod, "run_full_suite", _all_green)

    with Worktree(buggy_repo, run_id="v1") as wt:
        # max_attempts=1：让本轮就落到终局分支，好检查 results 里的中止原因
        st = new_state(buggy_repo, AifixConfig(max_attempts=1), run_id="v1")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["baseline_ids"] = [tid]
        st["_failures"] = _fs(tid).failures
        st["current"] = tid
        st["attempt"] = 1
        st["touched"] = []                  # 守卫记下：一个字节都没改
        st["abort_reason"] = "empty_diff"
        out = await verify_mod.verify_node(st)

    assert out["verdict"] == "same", "没有改动就不该判为修复"
    assert out["results"][0]["abort_reason"] == "empty_diff"


async def test_better_with_changes_still_commits(buggy_repo, monkeypatch):
    """有真实改动时行为不变。"""
    from aifix.config import AifixConfig
    from aifix.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes import verify as verify_mod
    from aifix.nodes.preflight import preflight_node

    tid = "tests/test_calc.py::test_add"

    async def _all_green(*a, **k):
        return _fs()

    monkeypatch.setattr(verify_mod, "run_full_suite", _all_green)

    with Worktree(buggy_repo, run_id="v2") as wt:
        (wt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                         encoding="utf-8")
        st = new_state(buggy_repo, AifixConfig(), run_id="v2")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["baseline_ids"] = [tid]
        st["_failures"] = _fs(tid).failures
        st["current"] = tid
        st["attempt"] = 1
        st["touched"] = ["calc.py"]
        out = await verify_mod.verify_node(st)

    assert out["verdict"] == "better"
