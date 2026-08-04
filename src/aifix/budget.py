"""三层预算：全局 → 单 failure → 单次 AgentLoop。

动态分配而非固定切分：前面省下来的额度自动流给后面难的。固定切分会
出现「最后一个 failure 明明有钱，却因为自己那份用完了而放弃」。

金额一律是**人民币**（价表按美元填时已在 consume 那一处折算完，见 money.py）。
"""
from __future__ import annotations

import time
from typing import Callable

from .money import fmt_cny


class RunBudget:
    FLOOR_TOKENS = 10_000        # 再紧也要给一次有意义尝试的余地

    def __init__(self, total_tokens: int, total_cny: float,
                 total_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._total_tokens = total_tokens
        self._total_cny = total_cny
        self._total_seconds = total_seconds
        self._clock = clock
        self._start: float | None = None
        self.spent_tokens = 0
        self.spent_cny = 0.0

    def start(self) -> None:
        if self._start is None:
            self._start = self._clock()

    def charge(self, tokens: int, cny: float) -> None:
        self.spent_tokens += tokens
        self.spent_cny += cny

    def remaining_tokens(self) -> int:
        return max(self._total_tokens - self.spent_tokens, 0)

    def for_failure(self, remaining_failures: int) -> int:
        """分给下一个 failure 的 token 额度。"""
        left = self.remaining_tokens()
        if remaining_failures <= 0:
            return left
        return max(left // remaining_failures, self.FLOOR_TOKENS)

    def remaining_cny(self) -> float:
        return max(self._total_cny - self.spent_cny, 0.0)

    def cny_for_failure(self, remaining_failures: int) -> float:
        """分给下一个 failure 的人民币额度。

        与 token 的 for_failure 同形状：动态分配而非固定切分，前面省下的
        自动流给后面难的。

        但**刻意没有下限**。token 那边设 FLOOR_TOKENS 是「再紧也要给一次
        有意义尝试的余地」；金额这边额度耗尽时若还给一个下限，闸就失效了
        —— 而「额度耗尽还在花」正是这个设计要挡住的事。
        """
        left = self.remaining_cny()
        if remaining_failures <= 0:
            return left
        return left / remaining_failures

    def exhaustion(self) -> tuple[str, str] | None:
        """超限返回 (种类, 原因)，未超返回 None。种类取值 tokens / cny / wall。

        种类必须能与消息分开取，因为三者的**归属**不同：

        - token 与金额预算是**模型**的属性。同一批任务、同一个上限，谁先
          烧完谁差 —— 「没在预算内修好」是被测系统的真实成绩，可比。
        - 墙钟预算是**评测调度器**的属性。`--parallel 8` 时八个任务在同一台
          机器上抢 CPU 跑全量 pytest，墙钟耗尽的概率远高于 `--parallel 1`；
          把它记成模型的失败，等于只改并行度就能改变修复成功率，直接违背
          跨模型对比的前提。

        种类曾经叫 `usd`，随结算货币改人民币一并改成 `cny`。老库里的行仍是
        `usd`，按种类聚合历史数据时两个都要认。
        """
        if self.spent_tokens >= self._total_tokens:
            return ("tokens",
                    f"token 预算耗尽：{self.spent_tokens} / {self._total_tokens}")
        if self.spent_cny >= self._total_cny:
            return ("cny", f"预算耗尽：{fmt_cny(self.spent_cny)}"
                           f" / {fmt_cny(self._total_cny)}")
        # 未 start 就不计时：单测直接构造 RunBudget 不该误触发时间中止
        if self._start is not None:
            elapsed = self._clock() - self._start
            if elapsed >= self._total_seconds:
                return ("wall", f"时间预算耗尽：{elapsed:.0f}s"
                                f" / {self._total_seconds:.0f}s")
        return None

    def exhausted(self) -> str | None:
        """超限返回原因，未超返回 None。"""
        hit = self.exhaustion()
        return hit[1] if hit else None
