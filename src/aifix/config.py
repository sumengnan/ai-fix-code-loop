from __future__ import annotations

from typing import Any

from harness.config import HarnessConfig
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AifixConfig(BaseSettings):
    """两条模型路由 + 预算 + 阈值。

    嵌套环境变量：AIFIX_DETECTOR__MODEL / AIFIX_FIXER__BASE_URL 等。
    """

    model_config = SettingsConfigDict(
        env_prefix="AIFIX_", env_nested_delimiter="__", extra="ignore")

    detector: HarnessConfig = Field(default_factory=HarnessConfig)
    fixer: HarnessConfig = Field(default_factory=HarnessConfig)

    # 模型价格表，形如 {"deepseek-v4-pro": [[1000000, 3, 6]]}
    # （区间上限、输入单价、输出单价）。不配就算不出成本 —— 报告里
    # 会明确写"未配置价格表"，而不是显示一个假的 $0.00。
    price_map: dict[str, Any] = Field(default_factory=dict)

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    fixer_max_steps: int = 25
    detector_max_tokens: int = 20_000
    loop_detect_window: int = 3
    tool_result_max_chars: int = 8000

    allow_test_edits: bool = False
