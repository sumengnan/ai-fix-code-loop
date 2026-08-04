# 配置

所有配置都通过**环境变量**给，前缀 `AIFIX_`，嵌套用两级下划线 `__`。
定义在 `src/aifix/config.py`（pydantic-settings）。

```bash
export AIFIX_FIXER__MODEL="qwen3-coder-flash"   # 嵌套：fixer.model
export AIFIX_BUDGET_CNY="15.0"                  # 扁平：budget_cny
```

> **一个必须知道的代价**：这个配置类用的是 `extra="ignore"` —— **拼错的配置名不会
> 报错，只会静默失效**。这是有意的：进程环境不归它管，上游镜像、CI runner、容器基座
> 随时会往里塞 `AIFIX_` 开头的变量，改成 `forbid` 等于把「别人多设了一个环境变量」
> 变成「所有人起不来」。

---

## 目录

- [最少要配的三样](#最少要配的三样)
- [模型路由](#模型路由)
- [价格表](#价格表)
- [货币](#货币)
- [测试环境](#测试环境)
- [预算](#预算)
- [迭代与守卫](#迭代与守卫)
- [复现那一步](#复现那一步)
- [issue 驱动](#issue-驱动)
- [其他](#其他)
- [已删除的旋钮](#已删除的旋钮)

---

## 最少要配的三样

```bash
export AIFIX_FIXER__BASE_URL="https://your-endpoint/v1"
export AIFIX_FIXER__API_KEY="sk-..."
export AIFIX_FIXER__MODEL="qwen3-coder-flash"
```

不配 detector 那条路由的话，它会走 `HarnessConfig` 的默认值（`gpt-4o-mini` +
`api.openai.com`），多半不是你想要的 —— 建议一起配上。

---

## 模型路由

两条独立路由，可以指向**两个不同的供应商**（诊断挑便宜的、修复挑强的，未必出自同一家）。
第三条 `reviewer` 只在打开裁判层时才需要，见下。

| 变量 | 说明 |
|---|---|
| `AIFIX_DETECTOR__BASE_URL` | 诊断模型的端点，要以 `/v1` 结尾 |
| `AIFIX_DETECTOR__API_KEY` | |
| `AIFIX_DETECTOR__MODEL` | |
| `AIFIX_FIXER__BASE_URL` | 修复模型的端点 |
| `AIFIX_FIXER__API_KEY` | |
| `AIFIX_FIXER__MODEL` | |

### 第三条路由：`reviewer`（裁判模型）

只在 `AIFIX_REVIEWER_CHECK=true` 时才需要，默认关。

```bash
export AIFIX_REVIEWER__BASE_URL="https://another-endpoint/v1"
export AIFIX_REVIEWER__API_KEY="sk-..."
export AIFIX_REVIEWER__MODEL="..."      # 建议**不要**和 fixer 用同一个
```

**打开了却没配这条路由的话，aifix 当场拒绝启动**，不会悄悄回退到 fixer 那条 ——
写补丁的和验补丁的是同一个模型时，两边的盲区、被 prompt 说服的方式、对「这算不算重构」
的判断全都一样，那样这一层看起来在工作、实际什么都没验，比不开更糟。

它和另外两条还有一处不同：**默认值是 `None`，不是一个构造出来的 `HarnessConfig`**。
`HarnessConfig` 自己的 env_prefix 是 `HARNESS_`，无条件构造一个的话，环境里任何一个格式
不对的 `HARNESS_*` 变量都会多出一条启动失败路径 —— 而「进程环境不归它管」正是这个配置类
选 `extra="ignore"` 的理由。

前两条路由的类型都是框架的 `HarnessConfig`，所以它的字段都能这样配
（`AIFIX_FIXER__TEMPERATURE`、`AIFIX_FIXER__REQUEST_TIMEOUT`、
`AIFIX_FIXER__MAX_RETRIES` 等）。

**启动前会探一次 fixer 那条路由**（发一个 `"ping"`，只读第一个 chunk）。探的是
fixer 不是 detector：detector 自己不通时仍会以「诊断解析失败」降级，那条路径本来就
有兜底；而真正干活也真正花钱的是 fixer。

### 一条安全提醒

配置类开了 `hide_input_in_errors=True`，这是**安全要求不是美观要求**。pydantic 默认
会把出错字段的 `input_value` 整个回显进异常消息，而嵌套路由那一层的 `input_value` 是
一整个 dict，里面躺着 api_key。

真实踩到过：`source` 了整份 `.env`，其中的 `HARNESS_*` 变量被嵌套的 `HarnessConfig`
（它的 env_prefix 正是 `HARNESS_`）一并吸走，一个值格式对不上就当场 ValidationError，
密钥前缀跟着进了 stderr。

---

## 价格表

```bash
export AIFIX_PRICE_MAP='{"qwen3-coder-flash": [0.0003, 0.0012]}'
```

格式是**扁平价表**：`{模型名: [输入价/千token, 输出价/千token]}`。

> 注意**不是**分档表（`[[上限, 输入, 输出], ...]`），两者不通用。传错格式会在**启动
> 阶段**就被拒绝 —— 成本计算是装饰性的，不该有崩掉一次跑到一半的 run 的权力。

**不配的后果**：`effective_cost` 恒为 0，所以

- 报告和对比表里会明写「未知（未配置 AIFIX_PRICE_MAP）」，**不会显示假的 `¥0.00`**
- **成本闸永远不会触发**

所以显式设了金额上限却没配价格表时，aifix **当场拒绝启动**：

```
拒绝启动：设置了预算上限，但没有配置价格表，这个上限不会生效。
  没有 price_map 时成本恒为 0，闸永远不触发 —— 与其给一个假的保证，不如现在就停。
```

在 GitHub Actions 上，价格表应该放 **variable 而不是 secret**：它不是机密，而 secret
在日志里会被遮成 `***`，你反而看不出它配没配对。

---

## 货币

**结算一律是人民币。** 预算、闸、报告、回放、trajectory 里的每一个金额都是 `¥`。

价表则两种都能填：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_PRICE_CURRENCY` | `USD` | 价表按哪种货币填：`USD` / `CNY` |
| `AIFIX_USD_TO_CNY` | 7.2 | 美元价表折人民币的汇率 |

默认 `USD` 是为了「抄过来就能用」—— 厂商公开的价目表几乎都是美元/千 token，逼着人
先乘一遍汇率再填，等于把一次手算错小数点的机会写进配置流程。手里拿的是国内厂商的
人民币报价时：

```bash
export AIFIX_PRICE_CURRENCY=CNY
export AIFIX_PRICE_MAP='{"deepseek-v4-pro": [0.002, 0.008]}'   # 元/千token
```

那时汇率一次都用不到。

**拼错会当场报错**，不会静默退回默认（这是 `extra="ignore"` 的少数例外之一）：
`AIFIX_PRICE_CURRENCY=RMB` 若被当成 `USD`，一份人民币价表会被再乘一遍汇率，账目整体
虚高 7 倍 —— 而闸照常工作、报告照常渲染，没有任何一处看得出不对。（`RMB` 实际被当作
`CNY` 接受，这里说的是任何认不出来的写法。）

### 汇率是个约数，不是实时值

它写死在配置里，报告的成本行会把它一起印出来：

```
- 成本：¥1.42（12,345 tokens，按 1 USD = 7.2 CNY 折算）
```

**为什么不联网取实时汇率**：这个项目的预算是拿来跨模型、跨时间对比的（同一批任务、
同一个上限，谁先烧完谁差）。汇率一动，同一批 eval 隔天跑出来的成本就不可比了 ——
那正是这套预算设计要避免的事。印出来是为了让读的人知道它是约数，不会拿它去对账。

### 折算只发生在一处

`agents/runner.consume` 把框架事件里的成本累加进 `AgentOutcome` 的那一行，全项目仅此
一处（见 `src/aifix/money.py`）。往下游任何一层再折一次，得到的都是一个看起来完全
正常的 7 倍。

---

## 测试环境

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_TEST_PYTHON` | 自动探测 | 跑**目标项目**测试用的解释器 |
| `AIFIX_TEST_TIMEOUT_SECONDS` | 1800 | 全量测试的超时 |
| `AIFIX_SCOPED_TEST_TIMEOUT_SECONDS` | 600 | 只跑几个用例时的超时 |
| `AIFIX_TEST_PARALLEL` | `auto` | 全量测试并行跑几个 worker（走 pytest-xdist） |
| `AIFIX_ALLOW_COLLECTION_ERRORS` | false | 允许「baseline 里收集错误占比过高」的仓库照常排队开修 |
| `AIFIX_ADAPTERS` | 自动探测第一个 | 显式指定跑哪几套测试，逗号分隔（如 `pytest,vitest`） |

### `AIFIX_ADAPTERS`

前后端同仓的工程（Python 后端 + vitest 前端）**一次 run 跑两套**：

```bash
export AIFIX_ADAPTERS=pytest,vitest
```

不设的话只用探测到的第一个 —— 与加这个开关之前完全相同的行为。

**为什么要人显式说一句，而不是探测到几个就跑几个**：`PytestAdapter.detect`
极宽松（有 `tests/` 或 `pyproject.toml` 就认领），而 Java 工程的工具链里带
Python 脚本（发版、代码生成、CI 胶水）是常事。自动全跑的话，那类仓库会凭空
多跑一套 pytest、收不到任何用例，然后被「测试没跑成」当场中止 —— 一个原本
工作正常的仓库在升级之后打不开。

而「该不该两套都跑」没有可靠的自动判据：前后端同仓与 Java 带 Python 胶水，
在探测那一层长得一模一样。分不清就不猜。

**只跑一套时另一侧的用例一条都不执行** —— 那不是「通过」，是不存在：baseline
看不见它们，verify 的三态比较也就永远不会因为它们变红而判 WORSE。

写错的名字在 preflight 就被拒（可选值见 `nodes/baseline.ADAPTERS`：`pytest` /
`maven` / `vitest`），不会等到 baseline 才炸 —— 那时的报错读起来像目标项目的
测试挂了。

两套测试**串行跑**，不并发：它们可能抢同一批端口、同一个测试数据库、同一个
临时目录。


### `AIFIX_TEST_PARALLEL`

**这是整个系统里最值钱的一处提速。** 一次 run 要跑好几遍全量（1 次 baseline + 每轮
verify 各 1 次），而它们此前是串行的。

实测（2026-08-03，本仓库 956 个用例，同一份代码跑两遍）：

```
串行   432s   (7分12秒)
-n auto 238s  (3分58秒)
```

一次 run 三遍全量：**21 分钟 → 12 分钟**。那次 28 分半的真跑（issue #9）里，绝大部分
就是这三遍。

取值：

| 值 | 含义 |
|---|---|
| `auto`（默认） | 目标解释器里探得到 pytest-xdist 就用 `-n auto`，探不到**静默串行** |
| 一个数字（`4`） | 原样发下去，不去探测。runner 上通常两三个就够，再多是抢 CPU |
| `off` / `0` / `1` / 空串 | 串行 |

`1` 也算串行：`-n 1` 会起一个 worker 进程，比不起还慢。空串必须当没设 —— Actions 里
`env: X: ${{ vars.Y }}` 在 Y 未设置时给的是空串，而 `-n ''` 会让 pytest 以
「argument -n: invalid int value」当场退出，表现成整个 baseline 跑不起来。

**只作用于全量，不作用于复跑。** 抖动确认那一跑就一两个用例，起 N 个 worker 是纯开销
—— xdist 要 fork 进程、收集、分发、汇总，而被分发的只有一个用例。

#### 代价：你的套件必须是 xdist-安全的

**xdist 会改变测试的执行顺序与进程隔离。** 目标项目的套件若依赖执行顺序、共享临时文件、
抢同一个端口，baseline 会多出一批本来不存在的红 —— 而那些红会进队列、**真花钱去修**。
表现不是崩溃，是「这个仓库怎么这么多失败」。撞上了就设 `AIFIX_TEST_PARALLEL=off`。

判定本身不受影响：baseline 与 verify 用的是同一条命令、同一个并行度，三态比较仍然是
**同一把尺量两次**。

> 默认设成 `auto` 是有依据的，不是想当然：本仓库 956 个用例上做过串行/并行对照，
> **「跑过的用例集」与「失败集」逐个相同**。但那只证明这一个仓库，证明不了你的。

### `AIFIX_TEST_PYTHON`

优先级：**显式配置 > 源仓库里的 `.venv/bin/python` 或 `venv/bin/python` > aifix 自己
的解释器**。

存在的理由是可用性：写死 `sys.executable` 等于要求目标项目的测试依赖装在 aifix 自己
的解释器里，而真实项目从不满足这一条。实测对照：拿 aifix 的 venv 去跑另一个项目的
测试 → 11 个 collection error，一个用例都没跑到；换它自己的 `.venv` → 673 passed。

**配了它就要知道这个陷阱**：目标项目若把自己可编辑安装（`pip install -e .`）进了那个
解释器，`import <目标包>` 可能解析到**源仓库**而不是 worktree 里那份打了补丁的代码 ——
测试照跑照绿，验证却完全失去意义。aifix 会在 baseline 之前做一次近似探测并往 stderr
出声，但那是提醒不是保证（它复现不了 `conftest.py` 里手写的 `sys.path` 改动）。

最可靠的自保是在目标项目的 pytest 配置里设 `pythonpath`：

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
```

它会把 worktree 的源码目录插到 `sys.path` 最前，盖过可编辑安装那条记录。

### 两个超时为什么分成两个旋钮

全量与局部差一到两个数量级。共用一个的话，要么局部等太久（挂死时白等半小时），
要么全量被局部的尺度掐掉。

1800 秒这个默认值是实测逼出来的：拿 aifix 自己当目标跑，套件在 worktree 里跑满
900 秒被杀，而那个 900 此前是写死在函数签名里的、config 里没有任何旋钮 ——
**任何测试套件超过 15 分钟的项目都直接不可用，且看不出为什么**。

### `AIFIX_ALLOW_COLLECTION_ERRORS`

默认关。那道闸拦的是「测试依赖没装在这个解释器里」这类环境故障 —— 把它们当工单排队
会真花钱，并把一次故障记成模型的失分。

它有真实的误判面：几个测试文件一起 import 不到同一个**仓库自己的**模块（改名、忘了
提交）时，那是个真 bug，值得修，而判据分不出它和「少装了一个第三方包」。没有这个
开关的话，那类仓库上 aifix 直接打不开。

打开时会往 stderr 出声并往 trace 记一条事实 —— 一条被静默绕过的守卫等于没有守卫。

---

## 预算

三层：**全局 → 单 failure → 单次 AgentLoop**。动态分配而非固定切分 —— 前面省下来的
额度自动流给后面难的，避免「最后一个 failure 明明有钱，却因为自己那份用完了而放弃」。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_BUDGET_CNY` | 15.0 | 整个 run 的人民币上限（需要价格表） |
| `AIFIX_BUDGET_TOKENS` | 500,000 | 整个 run 的 token 上限 |
| `AIFIX_BUDGET_WALL_SECONDS` | 1800 | 整个 run 的墙钟上限 |

三者任一越线即中止，而且**中止的种类分开记**：

- token / 金额耗尽 → 那是**模型的属性**。同一批任务、同一个上限，谁先烧完谁差 ——
  是被测系统的真实成绩，可比。
- 墙钟耗尽 → 那是**调度器的属性**。`--parallel 8` 时八个任务抢同一台机器的 CPU，
  墙钟耗尽的概率远高于 `--parallel 1`。记成模型的失败等于「只改并行度就能改变修复
  成功率」。

**预算耗尽退出码是 0**：活干到钱花完为止，结论仍然可信。

> **旧名 `AIFIX_BUDGET_USD` 仍然可用，且仍按美元读**（读到时会往 stderr 打一句提示，
> 并按 `AIFIX_USD_TO_CNY` 折成人民币）。不把它改读人民币，是因为那是一次**静默的 7 倍
> 缩水**：已经在 CI 里写着 `AIFIX_BUDGET_USD: 2.0` 的仓库会突然变成 ¥2 的上限，表现
> 是「模型最近怎么老是没修完就说预算耗尽」—— 一句没有人会联想到货币变更的报错。
> 两个都设会当场报错：谁生效只能靠猜。

token 那一层有一个下限 `FLOOR_TOKENS = 10_000`（再紧也要给一次有意义尝试的余地）；
**金额那一层刻意没有下限** —— 额度耗尽时若还给一个下限，闸就失效了。

---

## 迭代与守卫

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_MAX_ATTEMPTS` | 3 | 同一个 failure 最多试几轮 |
| `AIFIX_FIXER_MAX_STEPS` | 25 | 单次 AgentLoop 的步数上限 |
| `AIFIX_MAX_DIFF_LINES` | 300 | 单次修复允许的改动行数（+/- 合计），超过判为整文件重写 |
| `AIFIX_FIX_GUARD_RETRIES` | 2 | 守卫触发后额外给模型的重试次数（不计入 attempt） |
| `AIFIX_GUARD_GIVEUP_LIMIT` | 2 | **同一条**守卫连续触发多少次即放弃该 failure |
| `AIFIX_CONSECUTIVE_FAILURE_LIMIT` | 3 | 连续几个 failure 没修好就熔断整个 run |
| `AIFIX_LOOP_DETECT_WINDOW` | 3 | 框架的打转检测窗口 |
| `AIFIX_TOOL_RESULT_MAX_CHARS` | 8000 | 单次工具返回喂给模型的字符上限 |
| `AIFIX_DETECTOR_MAX_TOKENS` | 20,000 | 诊断那一步的 token 上限 |
| `AIFIX_FIXER_THINKING` | false | fixer 思考模式的**基准值**（会被下面那条升级盖过） |
| `AIFIX_FIXER_THINKING_AFTER_ATTEMPT` | 2 | 从第几轮起把思考模式升级成开；0 = 永不升级 |

### 思考模式：默认关，验证不过就升级

修 bug 的活大多是机械的（读代码、改几行、跑测试），而推理**按输出 token 计费**。
实测复现那一步有一轮的输出预算被推理全部吃掉、正文一个字没吐 —— fixer 走同一个端点、
同一个风险。所以第 1 轮走便宜的那一档。

第 2 轮起自动开。**判据是 `attempt` 而不是别的**，因为它的含义精确：`attempt` 只在
verify 判了 not-better **之后**才递增，所以 `attempt≥2` 就是「上一轮写出来的代码没通过
验证」—— 那才是值得花更多钱去想的时刻。

守卫重试（空 diff / 巨型 diff）**刻意不递增 `attempt`**，也就不会触发升级：那是「没写出
代码」，要的补救是把话说清楚（守卫的反馈文案），不是更强的推理。两者要的东西完全不同。

`AIFIX_FIXER_THINKING=`（空串）= 不发这个参数、随端点默认。**升级仍然生效** —— 「不表态」
不等于「不许升级」，而升级恰恰是要在这一刻明确表态。
| `AIFIX_ASK_USER` | true | 允不允许模型停下来问人 |

### `fix_guard_retries` 与 `guard_giveup_limit` 是咬在一起的

默认配置（retries=2 / giveup=2）下，**同一条守卫连撞两次就直接放弃，第 3 轮永远走不到**
—— 所以对「反复空 diff」这类最常见的情形，把 `FIX_GUARD_RETRIES` 调大**不会有任何效果**。

它只对**交替触发**（空 diff → 巨型 diff → 空 diff）有意义：那种情况下重复计数每次都被
重置，放弃规则不触发，多出来的轮数才用得上。

想让同一条守卫多撞几次，要调的是 `AIFIX_GUARD_GIVEUP_LIMIT`。

「同一条」而不是「任意守卫」也是刻意的：交替触发说明模型在换思路，值得再给一次；连续
两次空 diff 是同一堵墙撞两回。实测两个真实模型都在这里各烧了 51~52 万 token 却一个字
没改。

### `AIFIX_ASK_USER`

**没有人能回答的场合必须关掉。** `aifix eval` 已经硬编码把它设成 `False` —— 那边并行
跑几十个任务、没有任何人在看，留着它等于给模型一条烧钱的岔路。

带着答复重跑的那一轮也不会注册这个工具（答案就在开场白里，再问一次同样的问题是这条
路上最贵的失败方式）。

---

## 复现那一步

只在 `aifix reproduce` 和 issue 流水线里用到。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_REPRODUCER_MAX_STEPS` | 25 | 步数上限 |
| `AIFIX_REPRODUCER_MAX_TOKENS` | 250,000 | 独立的 token 上限，不吃整个 run 的额度 |
| `AIFIX_REPRODUCER_BUDGET_SHARE` | 0.4 | 最多能用掉整份金额预算的几成 |
| `AIFIX_REPRODUCER_THINKING` | false | 要不要开思考模式 |

### 步数为什么与 fixer 齐平

这个取值改过一次。最初设 12，理由是「reproducer 只有读工具，读够了就该作答」。实测
**两个模型都在 12 步用尽而不作答**，而回放显示它们没迷路 —— 有一个的第 12 步在读
`conftest.py` 弄清某个夹具，它在认真准备。

前提错在哪：fixer 拿到的是 traceback **加一份指名道姓的诊断**，只需确认那一处；
reproducer 拿到的是一段人话，要把整套测试脚手架逆推出来（命令、参数解析、这个仓库的
测试写法、夹具、替身）才写得出一条跑得起来的测试。**写复现比修 bug 需要更多探索，不是
更少。**

### token 上限必须与步数相称

`agent loop` 每一步都要把此前所有工具返回重发一遍，所以累计用量随步数**超线性**增长。
实测逐步累计：1.7k → 2.3k → 5.0k → 5.2k → 6.5k → 6.6k → 9.7k …，12 步撞在 66,721。

此前配 60k 的后果是**两个上限同时卡住**，失败消息说不清是哪个在限制 —— 而它们的下一步
动作不同（调步数 vs 换模型）。250k 是配合 25 步的余量，让步数成为唯一的约束。

### `budget_share` 是为了不让复现把修复饿死

实测过一次（当时的币种是美元）：复现把整份预算全吃光，`run_once` 拿到 0 当场中止，
报告只写「预算耗尽：0 / 0」—— 一句看不出是被前一步吃光的话。扣减本身是对的（不扣就
超支），错的是**没有分配**。

0.4 不是精算出来的：复现只跑一轮，修复要试 `max_attempts` 次，所以后者该拿大头。

### `reproducer_thinking` 默认关，但证据是不对称的

**为什么单给这一步一个开关**：它的活是机械的 —— 读代码、照抄这个仓库的测试写法、吐一段
JSON，不是需要长链推理的题。而实测有一轮的事件流是 **ReasoningDelta 1001 条、TextDelta
0 条**：模型把输出预算全烧在推理里，正文被截断，整轮零产出。

**但另一轮带着推理写出了一条质量很好的复现测试。** 所以「关掉更好」目前**没有对照实验
支撑**，默认关是一个取舍决定，不是一个测量结论。

取值：`false`（显式关）/ `true`（显式开）/ 空串（不发这个参数，随端点默认）。

> 在 GitHub Actions 里 `env: X: ${{ vars.Y }}` 在 Y 未设置时会把 X 设成**空串**而不是
> 不设 —— 而空串在这里的含义是「随端点默认」= 开，正好和「默认关」相反。所以 workflow
> 里写的是 `${{ vars.AIFIX_REPRODUCER_THINKING || 'false' }}`。

---

## issue 驱动

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_ALLOWED_USERS` | 空 | 额外获准触发 aifix 的 GitHub 登录名，逗号分隔 |

### `AIFIX_ALLOWED_USERS`

```bash
export AIFIX_ALLOWED_USERS="alice,bob"
# GitHub Actions 上：gh variable set AIFIX_ALLOWED_USERS --body "alice,bob"
```

它是**加法不是替换**。默认已经放行这几类人（见
[safety.md](safety.md#is_trusted触发权--已经能改这个仓库的人)）：

- 仓库账号本身
- `author_association` 为 `OWNER` / `COLLABORATOR`
- 组织仓库上的 `MEMBER`

这份名单只把按 `author_association` 认不出来的人**点名**放进来。

几处要知道的：

- **大小写不敏感** —— GitHub 的登录名本身就不区分，`Alice` 与 `alice` 是同一个人。
  区分的话，名单里差一个字母就是静默失效，而那道闸失效的表现是「他说他有权限，机器人却
  一直说他没有」。
- **整名匹配** —— `alice` 不会放行 `alicexyz`。裸子串匹配会把白名单变成前缀通行证。
- **它和仓库的真实权限会漂移** —— 人离职了、协作者移除了，名单还留着。长期授权应该走
  GitHub 自己的协作者机制，这份名单适合「某个具体的人，暂时」。
- 在 Actions 上用 **variable 不用 secret**：它不是机密，而 secret 在日志里会被遮成
  `***`，出问题时你看不出到底谁被放行了。

> 实现上它是 `authorize(payload, allowed_users=...)` 的**参数**，由 `AifixConfig` 读环境
> 后传进去 —— 那个函数是全项目最要紧的一道判定，保持纯函数才能被脱网穷举。

---

## 其他

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIFIX_ENABLE_CHECKPOINT` | false | 开启 LangGraph 的 SqliteSaver。会在产物目录下留一个 sqlite 文件 |

---

## 已删除的旋钮

### `AIFIX_ALLOW_TEST_EDITS`

**曾经存在，现已删除。** 它从一开始就没有被任何地方读过 —— 写入守卫里的测试文件检查
是无条件的。它不是回归掉的，是从来没接上过。

**不接线而是删掉**，因为这个项目的核心主张是「只有零 LLM 的确定性代码有资格说修好了」，
而**测试就是那个 oracle**。允许 agent 改测试等于允许它改判卷标准 —— 真实验收里模型把
`add` 改成有状态函数去满足一个自相矛盾的断言，已经证明它有多想走这条路。

一个没接线的危险旋钮比没有旋钮更糟：它给人「需要时可以打开」的错觉。真要开这个口子是
一次需要认真设计的改动（启动时的响亮警告、trace 里的显式记录、报告里的红字标注、评测里
单独一列），不是把一个 bool 接上去。

> 因为 `extra="ignore"`，`AIFIX_ALLOW_TEST_EDITS=true` 现在仍会被**静默吸收** ——
> 与删除之前一样不产生任何效果，没有变得更糟。

---

## 完整默认值速查

```python
detector / fixer               HarnessConfig()  # 框架默认，必须自己配
reviewer                       None             # 裁判模型；开了 reviewer_check 就必须配
price_map                      {}
allowed_users                  frozenset()      # AIFIX_ALLOWED_USERS
test_python                    None             # 自动探测
test_parallel                  "auto"           # 全量测试并行度
allow_collection_errors        False
test_timeout_seconds           1800.0
scoped_test_timeout_seconds    600.0
price_currency                 "USD"           # 价表按哪种货币填（USD / CNY）
usd_to_cny                     7.2              # 美元价表折人民币的汇率
budget_cny                     15.0
budget_tokens                  500_000
budget_wall_seconds            1800.0
max_attempts                   3
ask_user                       True
max_diff_lines                 300
necessity_check                True             # 交付前的补丁必要性反查
necessity_max_units            10               # 超过就整体跳过反查
fixer_thinking                 False            # 第 1 轮关推理
fixer_thinking_after_attempt   2                # 第 2 轮起升级成开
reviewer_check                 False            # 交付前让裁判模型复审（要花钱）
reviewer_max_tokens            20_000
fix_guard_retries              2
guard_giveup_limit             2
consecutive_failure_limit      3
fixer_max_steps                25
reproducer_max_steps           25
reproducer_max_tokens          250_000
reproducer_budget_share        0.4
reproducer_thinking            False
detector_max_tokens            20_000
loop_detect_window             3
tool_result_max_chars          8000
enable_checkpoint              False
```
