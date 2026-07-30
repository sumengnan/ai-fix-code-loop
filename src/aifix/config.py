from __future__ import annotations

from harness.config import HarnessConfig
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AifixConfig(BaseSettings):
    """两条模型路由 + 预算 + 阈值。

    嵌套环境变量：AIFIX_DETECTOR__MODEL / AIFIX_FIXER__BASE_URL 等。
    """

    # extra="ignore" 是**有意**的：这个类读的是进程环境，而进程环境不归它管。
    # 上游镜像、CI runner、容器基座随时会往里塞 AIFIX_ 开头的变量，改成
    # extra="forbid" 等于把「别人多设了一个环境变量」变成「所有人起不来」。
    # 代价必须写明：**拼错的配置名不会报错，只会静默失效**。已经删掉的字段
    # 同理 —— AIFIX_ALLOW_TEST_EDITS=true 至今仍会被安静吸收（见下方注释）。
    # 所以配置项改名/删除时，光删字段不够，得同步改文档。
    # hide_input_in_errors=True 是**安全**要求，不是美观要求：pydantic 默认会把
    # 出错字段的 input_value 整个回显进异常消息，而嵌套路由（detector / fixer）
    # 那一层的 input_value 是一整个 dict，里面躺着 api_key。真实踩到过 ——
    # source 了整份 .env，其中的 HARNESS_* 被嵌套的 HarnessConfig（它的
    # env_prefix 正是 HARNESS_）一并吸走，一个值格式对不上就当场 ValidationError，
    # 密钥前缀跟着进了 stderr。泄漏多少取决于 pydantic 对 repr 的截断长度，
    # 而那不是任何人承诺过的东西。见 tests/test_config.py 的哨兵测试。
    model_config = SettingsConfigDict(
        env_prefix="AIFIX_", env_nested_delimiter="__", extra="ignore",
        hide_input_in_errors=True)

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

    # 跑**目标项目**测试用的解释器（`AIFIX_TEST_PYTHON`）。不配就自动探测
    # 源仓库里的 `.venv/bin/python` / `venv/bin/python`，再找不到才退回
    # aifix 自己的 `sys.executable`。
    #
    # 它存在的理由是可用性：写死 sys.executable 等于要求**目标项目的测试依赖
    # 装在 aifix 自己的解释器里**，而真实项目从不满足这一条。实测拿 aifix 的
    # venv 去跑 ai-harness-framework 的测试：11 个 collection error，一个用例
    # 都没跑到；换它自己的 `.venv` 则 673 passed / 3 skipped。
    #
    # **配了它就要知道这个陷阱**：目标项目若把自己可编辑安装（pip install -e .）
    # 进了那个解释器，`import <目标包>` 可能解析到**源仓库**而不是 worktree 里
    # 那份打了补丁的代码 —— 测试照跑照绿，验证却完全失去意义。aifix 在 baseline
    # 之前会做一次近似探测并出声（见 adapters/pytest_adapter.imports_outside_worktree），
    # 但那是提醒，不是保证。
    test_python: str | None = None

    # 允许「baseline 里文件级收集错误占比过高」的仓库照常排队开修
    # （`AIFIX_ALLOW_COLLECTION_ERRORS`）。默认关：那种 baseline 绝大多数时候
    # 是环境坏了 —— 测试依赖没装在这个解释器里 —— 而把它当工单排队会真花钱，
    # 并把一次故障记成模型的失分（见 nodes/baseline.collection_error_abort）。
    #
    # 它**不是**一个没接线的旋钮（那种旋钮已经因为「给人可以打开的错觉」被
    # 删过一次，见文件末尾的 allow_test_edits）：baseline_node 真的读它，
    # 打开时还会往 stderr 出声并往 trace 记一条事实。
    #
    # 存在的理由是那道闸有真实的误判面：几个测试文件一起 import 不到同一个
    # **仓库自己的**模块（改名、忘了提交）时，那是个真 bug，值得修，而判据
    # 分不出它和「少装了一个第三方包」。没有这个开关的话，那类仓库上 aifix
    # 直接打不开，用户唯一的出路是去改 aifix 的源码。
    allow_collection_errors: bool = False

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    # 单次修复允许的改动行数上限（+/- 行合计）。超过即判为整文件重写：
    # 模型放弃理解、直接重写，那种补丁即使测试转绿也不该合。
    max_diff_lines: int = 300
    # 守卫触发后额外给模型的重试次数（不计入 max_attempts）。
    # 注意它与下面的 guard_giveup_limit 咬在一起：默认配置（retries=2 /
    # giveup=2）下，**同一条**守卫连撞两次就直接放弃，第 3 轮永远走不到 ——
    # 所以对「反复空 diff」这类最常见的情形，把这个值调大不会有任何效果。
    # 它只对**交替触发**（空 diff → 巨型 diff → 空 diff）有意义：那种情况
    # guard_repeats 每次都被重置，放弃规则不触发，多出来的轮数才用得上。
    # 想让同一条守卫多撞几次，要调的是 guard_giveup_limit。
    fix_guard_retries: int = 2
    # 同一条守卫连续触发多少次即放弃该 failure。用「同一条」而不是「任意
    # 守卫」：交替触发（空 diff → 巨型 diff）说明模型在换思路，值得再给一次；
    # 连续两次空 diff 是同一堵墙撞两回。实测两个真实模型都在这里各烧了
    # 51~52 万 token 却一个字没改。
    guard_giveup_limit: int = 2
    # 连着几个 failure 一个都没修好，大概率不是「这些 bug 恰好都难」，
    # 而是环境坏了 / prompt 崩了 / 今天这个模型不行。继续跑只是匀速烧钱。
    consecutive_failure_limit: int = 3
    fixer_max_steps: int = 25
    # 复现测试那一步的步数上限。**刻意小于 fixer_max_steps**：fixer 要 25 步
    # 是因为它靠 run_tests 的反馈来回迭代补丁；reproducer 只有读工具，读够了
    # 就该作答，多给的步数不会变成更好的测试，只会变成更长的翻阅。
    #
    # 这个字段是**实测逼出来的**：第一次真跑（2026-07-30，issue #1）沿用了
    # fixer 的 25 步，模型翻了 25 步没吐出 JSON，整轮以「达到 max_steps 上限」
    # 收场 —— 一次既没结论也没产出的空跑。
    reproducer_max_steps: int = 12
    # 复现这一步的 token 上限。**必须有**：它在 run_once 之外发起调用，如果不
    # 给独立上限，一次不收敛的复现能把整个 run 的额度吃掉大半。
    #
    # 实测（2026-07-30，issue #1 第二次真跑）：一次没产出任何结果的复现烧掉
    # 128,341 token —— 默认 500k 额度的四分之一，而它一个字的产出都没有。
    # 60k 的依据：detector 单步 20k，fixer 要反复迭代所以吃整份；reproducer
    # 读几个文件后作答，介于两者之间。
    reproducer_max_tokens: int = 60_000
    detector_max_tokens: int = 20_000
    loop_detect_window: int = 3
    tool_result_max_chars: int = 8000

    # 断点续跑：跑到一半崩掉能从上一个节点边界继续。默认关 ——
    # 它会在产物目录下留一个 sqlite 文件，按需开启。
    enable_checkpoint: bool = False

    # 这里曾有一个 allow_test_edits: bool = False，**已移除**：从 M1 起就没有
    # 任何地方读它，ApplyPatchTool._guard 的测试文件守卫是无条件的。它不是
    # 回归掉的，是从来没接上过。
    #
    # 不接线而是删掉，因为这个项目的核心主张是「只有零 LLM 的确定性代码有
    # 资格说修好了」，而**测试就是那个 oracle**。允许 agent 改测试等于允许它
    # 改判卷标准 —— M3 的真实验收里模型把 add 改成有状态函数去满足一个自相
    # 矛盾的断言，已经证明它有多想走这条路。一个没接线的危险旋钮比没有旋钮
    # 更糟：它给人「需要时可以打开」的错觉。真要开这个口子是一次需要认真设计
    # 的改动（启动时的响亮警告、trace 里的显式记录、报告里的红字标注、评测里
    # 单独一列），不是把一个 bool 接上去。理由见 docs/safety.md。
    #
    # 因为上面的 extra="ignore"，AIFIX_ALLOW_TEST_EDITS=true 现在仍会被静默
    # 吸收 —— 与删除之前一样不产生任何效果，没有变得更糟。
