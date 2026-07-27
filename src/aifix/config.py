from __future__ import annotations

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

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    fixer_max_steps: int = 25
    detector_max_tokens: int = 20_000
    loop_detect_window: int = 3
    tool_result_max_chars: int = 8000

    allow_test_edits: bool = False
