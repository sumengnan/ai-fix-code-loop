"""三态判定：系统里唯一有资格说「修好了」的地方，且不含任何 LLM。"""
from __future__ import annotations

from ..adapters.base import FailureSet, Verdict


def compare(baseline: FailureSet, current: FailureSet, target: str) -> Verdict:
    """把两次测试结果的差异归结为 BETTER / SAME / WORSE。

    new 的判断在最前：**即使目标用例修好了，只要引入任何新失败一律 WORSE**。
    比「净改善」更保守，但这正是敢在真实 repo 上跑的前提。
    """
    new = current.ids - baseline.ids
    if new:
        return Verdict.WORSE
    if target in (baseline.ids - current.ids):
        return Verdict.BETTER
    return Verdict.SAME
