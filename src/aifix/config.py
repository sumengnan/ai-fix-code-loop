from __future__ import annotations

from typing import Annotated

from harness.config import HarnessConfig
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # 第三条路由：裁判模型（agents/reviewer.py）。只在 `reviewer_check=True`
    # 时才会被用到。
    #
    # **默认 None 而不是 `default_factory=HarnessConfig`**，与上面两条不同 ——
    # 这不是风格差异，default_factory 有一个副作用：它会**无条件构造一个
    # HarnessConfig**，而 HarnessConfig 自己的 env_prefix 是 `HARNESS_`，于是
    # 环境里任何一个格式不对的 `HARNESS_*` 变量都会让 `AifixConfig()` 当场抛
    # SettingsError —— **哪怕 detector / fixer 两条路由都配全了、哪怕裁判这一层
    # 根本没打开**。detector / fixer 撞不上是因为实际使用中 `AIFIX_DETECTOR__*`
    # 总是配着的，那条路走的是环境值而不是 default_factory。
    #
    # 而「进程环境不归它管」正是这个类选 `extra="ignore"` 的理由（见上面那段）：
    # 让别人多设的一个变量把所有人挡在门外，与那条取舍直接冲突。
    #
    # 代价是打开 `reviewer_check` 却没配路由时它是 None —— 那种情况在启动时
    # 硬拒绝（`cli.require_reviewer_route`），**不退回 fixer 那条路由**：写补丁
    # 的和验补丁的是同一个模型时，两边的盲区、被 prompt 说服的方式、对「这算不算
    # 重构」的判断全都一样，那等于没验。悄悄回退到 fixer 会让这一层看起来在
    # 工作，实际上什么都没验 —— 比直接不开更糟。
    reviewer: HarnessConfig | None = None

    # 扁平价表：{模型名: [输入价/1k, 输出价/1k]}。注意**不是**分档表
    # （[[上限, 输入, 输出], ...]），两者不通用。不配就算不出成本 ——
    # 报告里会明写"未配置价格表"，而不是显示一个假的 $0.00。
    price_map: dict[str, list[float]] = Field(default_factory=dict)

    # 额外获准触发 aifix 的 GitHub 登录名（`AIFIX_ALLOWED_USERS`，逗号分隔）。
    #
    # 它是**加法**，不是替换：默认判据（仓库所有者 / 有写入权限的协作者 /
    # 组织仓库的成员，见 issue/event._is_trusted）照旧生效，这里只把按
    # `author_association` 认不出来的人点名放进来。
    #
    # 为什么需要它：`author_association` 只有 GitHub 预定义的那几档，而
    # `MEMBER` 对某个具体仓库未必有写入权限、`CONTRIBUTOR` 更不是信任信号。
    # 想精确到某几个人时，点名是唯一不引入网络调用的办法。
    #
    # **大小写不敏感**：GitHub 的登录名本身不区分大小写，Alice 与 alice 是同
    # 一个人。区分的话，名单里大小写差一个字母就是静默失效，而那道闸失效的
    # 表现是「他说他有权限，机器人却一直说他没有」。
    # `NoDecode` **不能省**（实测逼出来的）：pydantic-settings 对集合类型会先
    # 把环境变量按 JSON 解一遍，而那发生在 `mode="before"` 的验证器**之前**。
    # 不加它的话 `AIFIX_ALLOWED_USERS=alice,bob` 在读配置的那一刻就抛
    # JSONDecodeError，下面那个验证器一次都轮不到 —— 而没有人会在 repo
    # variable 里写 `["alice","bob"]`。
    allowed_users: Annotated[frozenset[str], NoDecode] = Field(
        default_factory=frozenset)

    @field_validator("allowed_users", mode="before")
    @classmethod
    def _split_logins(cls, v):
        """接受 `alice,bob` 这种写法，并统一 casefold。"""
        if isinstance(v, str):
            return {u.strip().casefold() for u in v.split(",") if u.strip()}
        if isinstance(v, (list, tuple, set, frozenset)):
            return {str(u).strip().casefold() for u in v if str(u).strip()}
        return v

    # mode="before"：抢在 pydantic 的类型强制之前跑，否则用户看到的是
    # "Input should be a valid number" 这类晦涩报错，而不是下面这句人话。
    # 空字符串当没设。GitHub Actions 里 `env: X: ${{ vars.Y }}` 在 Y 未设置时
    # 会把 X 设成**空串**而不是不设 —— 不接这一手，任何没配这个 variable 的仓库
    # 都会在启动时因为「'' 不是合法的 bool」而拒绝启动。
    @field_validator("reproducer_thinking", mode="before")
    @classmethod
    def _empty_means_unset(cls, v):
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none"):
            return None
        return v

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

    # 全量测试并行跑几个 worker（`AIFIX_TEST_PARALLEL`）。走 pytest-xdist。
    #
    # **这是整个系统里最值钱的一处提速**：一次 run 要跑好几遍全量（1 次
    # baseline + 每轮 verify 各 1 次），而它们是串行的。实测（2026-08-03，
    # issue #9 的真跑）：本仓库 944 个用例串行一遍 7 分 07 秒，一次 run 跑了
    # 28 分半，绝大部分就是那三遍。
    #
    # 取值：`"auto"`（xdist 探得到就用，探不到静默串行）/ 一个数字 / 空串或
    # `"off"`（串行）。默认 `"auto"`。
    #
    # **只作用于全量，不作用于复跑**：scoped 一次就跑一两个用例，起 N 个
    # worker 是纯开销（见 PytestAdapter.full_test_command）。
    #
    # 代价必须写明：**xdist 会改变测试的执行顺序与进程隔离**。目标项目的套件
    # 若不是 xdist-安全的（用例之间靠顺序、共享临时文件、抢同一个端口），
    # baseline 会多出一批本来不存在的红 —— 而那些红会进队列、真花钱去修。
    # 表现不是崩溃，是「这个仓库怎么这么多失败」。撞上了就设
    # `AIFIX_TEST_PARALLEL=off`。
    #
    # 判定本身不受影响：baseline 与 verify 用的是同一条命令、同一个并行度，
    # 三态比较仍然是同一把尺量两次。
    test_parallel: str | None = "auto"

    # 显式指定这个仓库要跑哪几套测试，逗号分隔（如 `pytest,vitest`）。
    # 不设时**只用探测到的第一个**，也就是加多适配器之前的行为。
    #
    # 为什么不自动「探测到几个就跑几个」：`PytestAdapter.detect` 极宽松 ——
    # 有 `tests/` 目录或 `pyproject.toml` 就认领，而 Java 工程的工具链里带
    # Python 脚本（发版、代码生成、CI 胶水）是常事。自动全跑的话，那类仓库
    # 会凭空多跑一套 pytest、收不到任何用例，然后被 `require_report` 判成
    # 「测试没跑成」当场中止 —— 一个今天能正常工作的仓库在升级之后打不开。
    #
    # 而「该不该两套都跑」**没有可靠的自动判据**：前后端同仓（pyproject.toml
    # + package.json）与 Java 带 Python 胶水，在探测那一层长得一模一样。
    # 分不清就不猜，让人说一句 —— 这比猜错之后静默少测一整套要好，也比猜错
    # 之后打不开要好。
    #
    #   export AIFIX_ADAPTERS=pytest,vitest
    #
    # 名字必须在注册表里（见 nodes.baseline.ADAPTERS），写错的名字在 preflight
    # 就被拒，不会等到 baseline 才炸。
    adapters: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("adapters", mode="before")
    @classmethod
    def _split_adapters(cls, v: Any) -> Any:
        """`"pytest,vitest"` → `("pytest", "vitest")`。

        要 `NoDecode` 的理由与 `allowed_users` 那条相同：pydantic-settings 会先
        拿环境变量去 JSON 解码，而 `pytest,vitest` 不是合法 JSON —— 不加的话
        配置一填就是 JSONDecodeError，且报错完全指不到点子上。
        """
        if isinstance(v, str):
            return tuple(x.strip() for x in v.split(",") if x.strip())
        return v

    # 跑目标项目测试的超时。**实测逼出来的**（2026-07-30，轮 9）：拿 aifix 自己
    # 当目标跑 `--dry-run`，套件在 worktree 里跑满 900 秒被杀，而这个数此前是
    # 写死在 `run_full_suite` 签名里的默认值，config 里没有任何旋钮 ——
    # **任何测试套件超过 15 分钟的项目都直接不可用，且看不出为什么**。
    #
    # 默认从 900 提到 1800：aifix 自己的套件本机约 10 分钟、CI 约 11 分钟，
    # 900 就压在边缘上，worktree 里缓存冷的时候必然越线。一个「刚好够用」的
    # 默认值等于把偶发失败写进产品。
    test_timeout_seconds: float = 1800.0
    # 只跑几个用例时的超时。分开一个旋钮而不是共用：全量与局部差一到两个数量级，
    # 共用的话要么局部等太久（挂死时白等半小时），要么全量被局部的尺度掐掉。
    scoped_test_timeout_seconds: float = 600.0

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    # 信息不全时允不允许 agent 停下来问人（`ask_user` 工具）。
    #
    # **没有人能回答的场合必须关掉**，`aifix eval` 就把它设成 False：那边并行
    # 跑几十个任务、没有任何人在看，留着它等于给模型一条烧钱的岔路 —— 把一
    # 整轮花在一个永远等不到回复的问题上，然后被判成没修好。
    #
    # 带着答复重跑的那一轮也不给（在 fix_node 里判），理由见那里。
    ask_user: bool = True
    # 单次修复允许的改动行数上限（+/- 行合计）。超过即判为整文件重写：
    # 模型放弃理解、直接重写，那种补丁即使测试转绿也不该合。
    max_diff_lines: int = 300

    # 交付前对补丁做必要性反查：逐个 hunk 反向、只重跑目标用例，撤掉之后目标
    # 照样绿的那些改动报进报告（见 necessity.py）。**只出信号，不改判定、不改
    # 交付内容。**
    #
    # 默认**开**，尽管它要跑测试 —— 一个默认关掉的信号等于没有这个信号，而它
    # 补的正是三条静态信号漏掉的那一类（改一个无关函数的返回值、新建一个谁也
    # 没 import 的文件，形状上一条都不亮）。成本被两条规则压住：只对**真正交付
    # 的补丁**跑，且只有一个单位时直接跳过 —— 绝大多数修复只有一个 hunk，那种
    # 情况下这一层一次测试都不跑。
    #
    # 关掉它的场合是真实存在的：单条 scoped 重跑很贵的项目（Maven 每次都要付
    # JVM 启动，`aifix eval` 并行几十个任务时更明显）。
    necessity_check: bool = True
    # 补丁拆出的单位数超过它就**整体跳过**反查，而不是「只查前 N 个」：
    # 半份名单在报告里读起来和完整名单一模一样，人会以为剩下的都查过且都必要。
    # 上界的意义是封住最坏情况的墙钟 —— N 个单位就是 N 次 scoped 重跑。
    necessity_max_units: int = 10
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
    # 复现测试那一步的步数上限。**与 fixer 齐平，不是更小** —— 这个取值改过
    # 一次，更正留在这里，因为最初那个前提是反的。
    #
    # 当初设 12 的理由：「reproducer 只有读工具，读够了就该作答，比 fixer 需要
    # 更少步数」。实测（2026-07-30，issue #2，flash 与 pro 各跑一轮）**两个模型
    # 都在 12 步用尽而不作答**，而回放显示它们没迷路：pro 的第 12 步在读
    # tests/conftest.py 弄清 buggy_repo 夹具 —— 它在认真准备。
    #
    # 前提错在哪：fixer 拿到的是 traceback **加一份指名道姓的诊断**，只需确认
    # 那一处；reproducer 拿到的是一段人话，要把整套测试脚手架逆推出来（命令、
    # 参数解析、这个仓库的测试写法、夹具、替身）才写得出一条**跑得起来**的测试。
    # **写复现比修 bug 需要更多探索，不是更少。**
    reproducer_max_steps: int = 25
    # 复现这一步的 token 上限。**必须有**：它在 run_once 之外发起调用，如果不
    # 给独立上限，一次不收敛的复现能把整个 run 的额度吃掉大半。
    #
    # 实测（2026-07-30，issue #1 第二次真跑）：一次没产出任何结果的复现烧掉
    # 128,341 token —— 默认 500k 额度的四分之一，而它一个字的产出都没有。
    # **这个数必须与 reproducer_max_steps 相称**，两个旋钮不是独立的：agent loop
    # 每一步都要把此前所有工具返回重发一遍，所以累计用量随步数**超线性**增长。
    #
    # 实测（2026-07-30，issue #2，deepseek-v4-flash，带 offset 的 read_file）：
    # 逐步累计 1.7k → 2.3k → 5.0k → 5.2k → 6.5k → 6.6k → 9.7k …，12 步撞在
    # 66,721。此前配 60k 的后果是**两个上限同时卡住**，失败消息说不清是哪个在限
    # 制——而它们的下一步动作不同（调步数 vs 换模型）。
    #
    # 250k 是配合 25 步的余量，让 steps 成为唯一的约束：那样「用满步数没答出来」
    # 才是一句干净的结论。代价要说清楚 —— 按 pro 的价目这一步跑满约 $1，但它
    # **会从后面修复那一步的额度里扣掉**（见 issue/handle.py 里那段），所以花的
    # 不是额外的钱，是同一份预算里更靠前的一段。
    reproducer_max_tokens: int = 250_000
    # 复现这一步最多能用掉整份预算的几成。**没有这一条，扣减会把修复步饿死。**
    #
    # 实测（2026-07-30，issue #2，pro 跑 25 步）：复现把 AIFIX_BUDGET_USD=0.50
    # 全吃光，run_once 拿到 $0、当场中止，报告只写「美元预算耗尽：$0 / $0」——
    # 一句看不出是被前一步吃光的话。扣减本身是对的（不扣就超支），错的是**没有
    # 分配**：没有任何东西保证后面那一步还剩下钱。
    #
    # 0.4 不是精算出来的：复现只跑一轮，修复要试 max_attempts 次，所以后者该
    # 拿大头。真正的依据要等一批任务的数据，那时这个数应该按实测重定。
    reproducer_budget_share: float = 0.4
    # 复现这一步要不要开思考模式。**默认关**。
    # None = 不发这个参数（由端点自己的默认决定），True / False 则显式指定。
    #
    # 为什么单给这一步一个开关：它的活是**机械**的 —— 读代码、照抄这个仓库的
    # 测试写法、吐一段 JSON，不是需要长链推理的题。而实测（2026-07-30，issue #2，
    # deepseek-v4-pro）有一轮的事件流是 **ReasoningDelta 1001 条、TextDelta 0 条**：
    # 模型把输出预算全烧在推理里，正文被截断，整轮零产出。同一天的连通性探针也
    # 佐证了这个端点的默认是开着的 —— `max_tokens:1` 下那唯一一个 token 进的是
    # `reasoning_content`，`content` 是空串。
    #
    # **证据是不对称的，这一点必须写明**：另一轮带着推理写出了一条质量很好的
    # 复现测试。所以「关掉更好」目前**没有对照实验支撑**，默认关是一个取舍
    # 决定（作者定的），不是一个测量结论。真要下结论，需要开/关各跑一批任务
    # 比修复成功率 —— 那个读数还不存在。
    #
    # 想恢复成「不发参数、随端点默认」：`AIFIX_REPRODUCER_THINKING=`（空串）。
    #
    # 落到线上是 harness 的 `_adapt_thinking`：模型名含 deepseek 时翻成
    # `thinking={"type": "disabled"}`；Qwen/百炼那边原样发 enable_thinking。
    reproducer_thinking: bool | None = False
    detector_max_tokens: int = 20_000

    # 交付前让裁判模型复审一遍补丁（agents/reviewer.py）。**只出信号，不改判
    # 定、不挡交付。**
    #
    # 默认**关**，与必要性反查相反。三条理由：
    # 1. 它要发一次真实的模型调用，花的是钱不是墙钟，而这个项目的其它信号层
    #    全是零成本的 —— 默认给所有人加一笔开销不合适。
    # 2. 它的产出是这几层里唯一**不可复现**的：同一个补丁两次跑可能给不同的
    #    判断。默认开着会让「报告为什么这次多了一节」变成一个没法回答的问题。
    # 3. 它只在配了独立的 `reviewer` 路由（最好是与 fixer 不同的模型）时才有
    #    意义，而那是一个需要人显式做的决定。
    reviewer_check: bool = False
    reviewer_max_tokens: int = 20_000
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
