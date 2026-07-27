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
