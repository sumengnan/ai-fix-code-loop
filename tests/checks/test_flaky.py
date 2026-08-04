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
    from aifix.runtime.delivery import Worktree
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
    from aifix.runtime.delivery import Worktree
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


class _SilentAdapter:
    """一个测试进程跑不出报告的适配器：超时被杀 / 崩溃 / 依赖没装。

    只实现 verify 这条路径用得到的部分，其余委托给真的 PytestAdapter。
    """

    def __init__(self):
        from aifix.adapters.pytest_adapter import PytestAdapter
        self._real = PytestAdapter()
        self.name = "pytest"

    def __getattr__(self, item):
        return getattr(self._real, item)

    def full_test_command(self):
        import sys
        return [sys.executable, "-c", ""]

    def scoped_test_command(self, test_ids):
        import sys
        return [sys.executable, "-c", ""]


async def test_verify_refuses_to_read_a_dead_test_run_as_all_green(
        buggy_repo, monkeypatch):
    """verify 的全量跑没产出报告时必须抛，绝不能判 BETTER。

    这是「没跑成冒充全绿」三处里最贵的一处：空的 current 集合意味着
    「目标用例不再失败、也没有新失败」，compare 直接判 BETTER，于是
    **一个从未被验证过的补丁被 commit 进交付分支**，报告写「已修复」。
    「系统里唯一有资格说修好了的组件是最笨的那个」这条主张就是在这里被
    击穿的——最笨的那个压根没开口，判定却照做了。
    """
    from aifix.config import AifixConfig
    from aifix.runtime.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes import verify as verify_mod
    from aifix.nodes.preflight import preflight_node

    import pytest as _pytest
    tid = "tests/test_calc.py::test_add"
    monkeypatch.setattr(verify_mod, "adapters_from_state",
                        lambda state: [_SilentAdapter()])

    with Worktree(buggy_repo, run_id="v3") as wt:
        (wt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                         encoding="utf-8")
        st = new_state(buggy_repo, AifixConfig(), run_id="v3")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["baseline_ids"] = [tid]
        st["_failures"] = _fs(tid).failures
        st["current"] = tid
        st["attempt"] = 1
        st["touched"] = ["calc.py"]
        with _pytest.raises(RuntimeError, match="报告"):
            await verify_mod.verify_node(st)


async def test_flaky_rerun_that_never_ran_is_not_evidence_of_flakiness(
        buggy_repo, monkeypatch):
    """复跑没产出报告时不能把确认回归判成抖动。

    filter_flaky 拿空集合当「重跑就绿」：新增失败全部被划进 flaky、从
    effective 里剔除，判定回到 BETTER —— 一个**真的弄红了别的用例**的补丁
    被提交。三处里语义最反的一处：它不只是没验证，是把反证据当成了正证据。
    """
    from aifix.config import AifixConfig
    from aifix.runtime.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes import verify as verify_mod
    from aifix.nodes.preflight import preflight_node

    import pytest as _pytest
    tid = "tests/test_calc.py::test_add"

    async def _full_with_a_new_failure(*a, **k):
        return _fs(tid, "tests/test_calc.py::test_identity")

    # 全量跑正常产出（有一个新失败），只有复跑那一步跑不出报告
    monkeypatch.setattr(verify_mod, "run_full_suite", _full_with_a_new_failure)
    monkeypatch.setattr(verify_mod, "adapters_from_state",
                        lambda state: [_SilentAdapter()])

    with Worktree(buggy_repo, run_id="v4") as wt:
        (wt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                         encoding="utf-8")
        st = new_state(buggy_repo, AifixConfig(), run_id="v4")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["baseline_ids"] = [tid]
        st["_failures"] = _fs(tid).failures
        st["current"] = tid
        st["attempt"] = 1
        st["touched"] = ["calc.py"]
        with _pytest.raises(RuntimeError, match="报告"):
            await verify_mod.verify_node(st)
