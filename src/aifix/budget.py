"""三层预算：全局 → 单 failure → 单次 AgentLoop。

动态分配而非固定切分：前面省下来的额度自动流给后面难的。固定切分会
出现「最后一个 failure 明明有钱，却因为自己那份用完了而放弃」。
"""
from __future__ import annotations

import time
from typing import Callable


def fmt_usd(v: float) -> str:
    """小额不四舍五入成 $0.00 —— `--budget 0.001` 显示成上限 $0.00 像个 bug。"""
    return f"${v:.2f}" if abs(v) >= 0.01 else f"${v:g}"


class RunBudget:
    FLOOR_TOKENS = 10_000        # 再紧也要给一次有意义尝试的余地

    def __init__(self, total_tokens: int, total_usd: float,
                 total_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._total_tokens = total_tokens
        self._total_usd = total_usd
        self._total_seconds = total_seconds
        self._clock = clock
        self._start: float | None = None
        self.spent_tokens = 0
        self.spent_usd = 0.0

    def start(self) -> None:
        if self._start is None:
            self._start = self._clock()

    def charge(self, tokens: int, usd: float) -> None:
        self.spent_tokens += tokens
        self.spent_usd += usd

    def remaining_tokens(self) -> int:
        return max(self._total_tokens - self.spent_tokens, 0)

    def for_failure(self, remaining_failures: int) -> int:
        """分给下一个 failure 的 token 额度。"""
        left = self.remaining_tokens()
        if remaining_failures <= 0:
            return left
        return max(left // remaining_failures, self.FLOOR_TOKENS)

    def exhausted(self) -> str | None:
        """超限返回原因，未超返回 None。"""
        if self.spent_tokens >= self._total_tokens:
            return f"token 预算耗尽：{self.spent_tokens} / {self._total_tokens}"
        if self.spent_usd >= self._total_usd:
            return (f"美元预算耗尽：{fmt_usd(self.spent_usd)}"
                    f" / {fmt_usd(self._total_usd)}")
        # 未 start 就不计时：单测直接构造 RunBudget 不该误触发时间中止
        if self._start is not None:
            elapsed = self._clock() - self._start
            if elapsed >= self._total_seconds:
                return f"时间预算耗尽：{elapsed:.0f}s / {self._total_seconds:.0f}s"
        return None
