from aifix.adapters.base import Failure, FailureSet, Verdict
from aifix.verify import compare


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


def test_target_fixed_is_better():
    assert compare(_fs("a", "b"), _fs("b"), "a") is Verdict.BETTER


def test_nothing_changed_is_same():
    assert compare(_fs("a", "b"), _fs("a", "b"), "a") is Verdict.SAME


def test_new_failure_is_worse():
    assert compare(_fs("a"), _fs("a", "c"), "a") is Verdict.WORSE


def test_target_fixed_but_regression_is_worse():
    """核心约束：即使目标修好了，引入任何新失败一律 WORSE。"""
    assert compare(_fs("a", "b"), _fs("b", "c"), "a") is Verdict.WORSE


def test_other_failure_fixed_but_target_not_is_same():
    """顺手修好别的、目标没修好 —— 不算 BETTER。"""
    assert compare(_fs("a", "b"), _fs("a"), "a") is Verdict.SAME


def test_all_fixed_is_better():
    assert compare(_fs("a", "b"), _fs(), "a") is Verdict.BETTER
