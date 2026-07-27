from __future__ import annotations

from harness.config import HarnessConfig
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AifixConfig(BaseSettings):
    """两条模型路由 + 预算 + 阈值。

    嵌套环境变量：AIFIX_DETECTOR__MODEL / AIFIX_FIXER__BASE_URL 等。
    """

    model_config = SettingsConfigDict(
        env_prefix="AIFIX_", env_nested_delimiter="__", extra="ignore")

    detector: HarnessConfig = Field(default_factory=HarnessConfig)
    fixer: HarnessConfig = Field(default_factory=HarnessConfig)

    # 扁平价表：{模型名: [输入价/1k, 输出价/1k]}。注意**不是**分档表
    # （[[上限, 输入, 输出], ...]），两者不通用。不配就算不出成本 ——
    # 报告里会明写"未配置价格表"，而不是显示一个假的 $0.00。
    price_map: dict[str, list[float]] = Field(default_factory=dict)

    # mode="before"：抢在 pydantic 的类型强制之前跑，否则用户看到的是
    # "Input should be a valid number" 这类晦涩报错，而不是下面这句人话。
    @field_validator("price_map", mode="before")
    @classmethod
    def _price_map_must_be_flat(cls, v: dict) -> dict:
        """加载时就拒绝错误格式。

        分档表传进来时，框架的 cost_usd 会在解包处抛 ValueError ——
        而那已经是跑到一半、token 花掉之后了。成本计算是装饰性的，
        不该有崩掉整个 run 的权力，所以把它拦在启动阶段。
        """
        for model, price in v.items():
            if len(price) != 2:
                raise ValueError(
                    f"price_map['{model}'] 需要扁平价表 [输入价/1k, 输出价/1k]，"
                    f"得到 {price!r}。分档表 [[上限,输入,输出], ...] 不是这个格式。")
        return v

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    # 单次修复允许的改动行数上限（+/- 行合计）。超过即判为整文件重写：
    # 模型放弃理解、直接重写，那种补丁即使测试转绿也不该合。
    max_diff_lines: int = 300
    # 守卫触发后额外给模型的重试次数（不计入 max_attempts）
    fix_guard_retries: int = 2
    # 连着几个 failure 一个都没修好，大概率不是「这些 bug 恰好都难」，
    # 而是环境坏了 / prompt 崩了 / 今天这个模型不行。继续跑只是匀速烧钱。
    consecutive_failure_limit: int = 3
    fixer_max_steps: int = 25
    detector_max_tokens: int = 20_000
    loop_detect_window: int = 3
    tool_result_max_chars: int = 8000

    # 断点续跑：跑到一半崩掉能从上一个节点边界继续。默认关 ——
    # 它会在产物目录下留一个 sqlite 文件，按需开启。
    enable_checkpoint: bool = False

    allow_test_edits: bool = False
