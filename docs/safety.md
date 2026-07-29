# 安全边界：它凭什么敢跑在你自己的仓库上

这份文档回答一个问题：**一个随机的模型拿到了写文件的能力，什么东西拦着它不把你的仓库改坏、不把你的钱烧光。**

流程与节点职责见[架构](architecture.md)。

---

## 四层封闭

四层是**串联**的，不是四选一。任意一层被绕过，后面几层仍然拦得住。

### 一、能力：白名单工具面，没有 `run_shell`

Fixer 的 `ToolRegistry` 里只注册五个工具（`src/aifix/agents/fixer.py`）：

| 工具 | 能干什么 |
|---|---|
| `read_file` / `list_files` | 只读，框架自带 |
| `grep` | 底层是 `git grep`，自动跳过 `.gitignore` 里的路径 |
| `apply_patch` | **唯一的修改手段**，只接 unified diff，不接整文件覆写 |
| `run_tests` | 只能跑当前失败列表里的用例，一次最多 5 个，不能跑全量 |

`ai-harness-framework` 自带 `RunShellTool`（`name = "run_shell"`），**没有被注册**。这不是靠注释保证的 —— `tests/test_fixer.py` 里有两条测试钉住它：一条断言注册表里的工具名集合恰好是上面那五个，另一条断言 `reg.get("run_shell") is None` 且 `reg.get("run_python") is None`。

`run_tests` 的白名单（`known_ids`）也是能力面的一部分：模型给一个不在失败列表里的 id 会拿到 `ToolError` 和完整的可用列表，而不是让它自己去发明测试选择器。

Detector 那一路更彻底：`ToolRegistry()` 是**空的**，`max_steps=1`，模型必然一步出文本。

### 二、路径：`resolve_in_workspace`

`apply_patch` 与 `grep` 在动手之前都过一次 `harness.sandbox.base.resolve_in_workspace`，逃逸工作区抛 `SandboxError`（在 `apply_patch` 里被转成 `ToolError` 回给模型）。

`apply_patch` 另外拒绝 `.git` 目录下的任何路径。

### 三、进程：cwd

所有子进程都由 `LocalSandbox(workspace=<worktree 路径>)` 启动，cwd 是 worktree。测试命令、`git apply`、`git diff`、`git ls-files` 全部在那里跑。

pytest 那条命令的 argv[0] 是**目标项目自己的解释器**（`AIFIX_TEST_PYTHON`，或探测到的源仓库 `.venv/bin/python`；见[适配器](adapters.md#用哪个解释器跑-pytest)）。cwd 仍然是 worktree —— 解释器换了，被跑的代码没换。这一条正是下面「已知的局限」里那个陷阱的着力点：解释器带着自己的 site-packages 进来，而那里面可能有一条指回**源仓库**的路。

### 四、git：worktree + 分支

改动只发生在 `<repo>/.aifix/runs/<run_id>/tree`，那是从 HEAD 创建的独立 worktree，挂在分支 `aifix/<run_id>` 上。主工作区**绝不被触碰**。

`preflight` 会拒绝在不干净的工作区上启动（只看已跟踪文件，见[架构](architecture.md)）。

交付时**绝不用 `git add -A`**：worktree 里跑过测试会产生一堆未跟踪产物，全扫进去会污染交付分支。`Worktree.commit` 只 `git add -- <ApplyPatchTool 记账过的路径>` —— 因为 `apply_patch` 是 agent 唯一的修改手段，「改过哪些」是**已知的**，不需要靠「把工作区里变了的东西全扫进来」去猜。

---

## 守卫

每条守卫的完整形态：**它挡的是什么** / **默认阈值** / **绕过它会发生什么**。

### 不许改测试文件

- **挡什么**：模型删掉断言让测试变绿。这是这个系统最核心的一道守卫。
- **在哪**：`ApplyPatchTool._guard`（`src/aifix/tools/patch.py`）
- **阈值**：没有阈值，无条件拒绝。判据是 `signals.under_dirs(path, adapter.test_dirs())`
- **绕过的后果**：整个系统失去意义 —— 报告会写「已修复」，而补丁删的是断言。
- **没有开关，这是有意的**：`AifixConfig` 里曾有一个 `allow_test_edits: bool = False`，从 M1 起就没有被任何地方读过（守卫一直是无条件的），已删除。删而不接，因为测试是这个系统判「修好了」的 **oracle** —— 允许 agent 改测试等于允许它改判卷标准。一个没接线的危险旋钮比没有旋钮更糟：它给人「需要时可以打开」的错觉。真要开这个口子是一次需要认真设计的改动（启动时的响亮警告、trace 里的显式记录、报告里的红字标注、评测里单独一列），不是把一个 bool 接上去。`tests/test_config.py::test_已移除的_allow_test_edits_不会复活` 钉住它不再回来。

它的历史值得单独一节，见下面[「守卫本身也需要被攻击性地测试」](#守卫本身也需要被攻击性地测试)。

### 空 diff（`empty_diff`）

- **挡什么**：模型只说「已修复」而一个字节都没改
- **判据**：`_diff_lines(sandbox, touched) == 0`（`src/aifix/nodes/fix.py`）
- **处理**：带反馈重试，不是直接失败
- **绕过的后果**：`verify` 那一层还有一道 —— 判 BETTER 但 `touched` 为空会被降级成 `SAME`，因为那说明目标用例在 baseline 里本来就是抖的

行数只统计 `touched` 里的路径。worktree 里跑过测试会留下一堆未跟踪产物（`__pycache__`、覆盖率文件、日志），把它们算进来等于让这道守卫**从此永不触发**。

有一个明确的代价：`git ls-files --others --exclude-standard` 让被 `.gitignore` 盖住的新文件恒计 0 行。这是**有意**保留的（那个文件交付不了，`git add` 对被忽略的路径直接以 1 退出），但反馈文案必须跟上 —— 这种情况下发的是 `_IGNORED_FEEDBACK`（「你的改动落在被 .gitignore 忽略的路径上」）而不是 `_EMPTY_FEEDBACK`（「你没有对任何文件做出修改」）。后者是**一句假话**，模型照着它做只会再改一次同样的东西，一路重试到放弃。守卫种类仍记 `empty_diff`（可交付的改动确实是零），但另记一条 `ignored_paths` 事实，复盘时两种零分得开。

### 巨型 diff（`huge_diff`）

- **挡什么**：模型放弃理解、直接整文件重写。那种补丁即使测试转绿也不该合
- **阈值**：`max_diff_lines`，默认 **300**（+/- 行合计）
- **处理**：**立刻回滚**（`git checkout -- .` + `git clean -fd`）、清空 `touched`、带反馈重试
- **绕过的后果**：交付分支上出现无法 review 的补丁

回滚之后 `lines` 必须清零。不清零的话，守卫用尽时记进 trace 的是回滚前的陈旧值 —— 观测数据撒谎比没有观测更糟。

### 守卫连撞放弃（`<kind>_giveup`）

- **挡什么**：模型撞同一堵墙撞到把额度烧光
- **阈值**：`guard_giveup_limit`，默认 **2**（**同一条**守卫连续触发 2 次即放弃该 failure）
- **实测**：两个真实模型都在这里各烧了 51~52 万 token 却一个字没改

用「同一条」而不是「任意守卫」是有理由的：交替触发（空 diff → 巨型 diff）说明模型在换思路，值得再给一次；连续两次空 diff 是同一堵墙撞两回。

**它与 `fix_guard_retries`（默认 2）咬在一起，这一点容易踩坑**：守卫重试循环跑 `range(fix_guard_retries + 1)` 共 3 轮，而同一条守卫第 2 轮就触发放弃 —— **第 3 轮永远走不到**。所以对「反复空 diff」这类最常见的情形，把 `fix_guard_retries` 调大**不会有任何效果**。它只对交替触发有意义（那种情况计数每次被重置）。想让同一条守卫多撞几次，要调的是 `guard_giveup_limit`。

### 回归回滚

- **挡什么**：补丁修好了目标用例却弄坏了别的
- **判据**：`verify.compare` —— 出现**任何**新失败一律 `WORSE`，即使目标用例修好了
- **处理**：`Worktree.rollback()`

`rollback` 先 `git reset -q` 再 `git checkout -- .`：暂存了但没提交的内容同样属于「未提交的改动」。少这一句时 `git checkout -- .` 是**从索引**往工作区拷，半个暂存区会被原样还原回工作区，等于没回滚。这不是理论边界 —— 新文件命中 `.gitignore` 时 `git add` 以 1 退出而同一条命令里别的路径已经暂存了（实测），随后的回滚就落在这个状态上。

### flaky 过滤

- **挡什么**：抖动的测试把一个本来正确的补丁滚掉。**这是这个系统最昂贵的错误**
- **判据**：只在出现新失败时触发，且只重跑那几个用例（`filter_flaky`，`src/aifix/nodes/verify.py`）
- **成本**：近似为零
- **绕过的后果**：正确的修复被误判成 `WORSE` 并丢弃

### baseline 全是收集错误（`collect` 中止）

- **挡什么**：把「这台机器上缺了点什么」当成待修用例排队。pytest 收集阶段整轮中断时**照样写出一份完整的 JUnit 报告**，里面是一条条文件级 `<error>` —— 它们被 `make_test_id` 老老实实翻译成可重跑的 node id，进 `baseline_ids`，进 `queue`。放任不管的话，Detector 和 Fixer 会被派去修一件它们改不了的事：真花钱；连续失败熔断在第 3 个之后中止；而报告里那几行写的是「模型没修好」——**一个成绩，其实是一次故障**。走评测的话会被记成模型的失分
- **判据**：`collection_error_abort()`（`src/aifix/nodes/baseline.py`）。文件级 id 的条数 **≥ 2** 且 **严格过半** 才中止。「哪些 id 是文件级」问适配器的 `is_file_level_id`，不写死 pytest 的 `::`（`eval/mine` 曾写死过，后果是每一个 Maven id 都被判成文件级）
- **两条阈值各挡一件事**：条数下限挡「小样本上的比例没有意义」—— 单独一条收集错误极可能就是这个仓库自己的 bug（模块被改名、忘了提交一个文件），那正是 aifix 该修的活，且代价有界（队列里就它一条）；比例下限挡「个别文件导不进来被误当成环境故障」
- **两条阈值在两个适配器上做功不同**：**pytest 只要有一条收集错误就中断整轮收集**（实测 pytest 9.1.1，exit 2，报告里只剩那几条文件级 `<error>`，别的测试一个都没跑），所以 pytest 侧的占比在有收集错误时**恒为 1.0**，真正做功的只有条数那一条；比例那一条是给 Maven 用的 —— surefire 不中断，一个测试类 `@BeforeAll` 炸了别的类照跑，类级 error 与用例级失败**真会**混在一起
- **中止而不是过滤**：过滤掉文件级 id、让「还剩的真失败」照修，在 pytest 上必然得到一个空队列（收集一中断，报告里除了这些 error 什么都没有），run 会以「修复 0 / 0、全绿、没活干」收场 —— 正是这条守卫要消灭的那副样子。更根本的是收集错误意味着**这份 baseline 是残缺的**，悄悄扔掉几条 id 等于把「测量没做成」伪装成「测量做完了，只是少几条」
- **看得出来是故障，不是成绩**：报告里的「修复 x / y」这一行被换成「—（baseline 不可信，一个用例都没开修）」；退出码 **1**（预算耗尽退 0 —— 那是正常收场，结论仍然可信）；评测侧 `abort_kind == "collect"` 与墙钟中止同类，走**评测故障**，不进修复成功率的分母
- **绕过**：`AIFIX_ALLOW_COLLECTION_ERRORS=1`。它有真实的用途 —— 几个测试文件一起 import 不到同一个**仓库自己的**模块时那是个真 bug，而判据分不出它和「少装了一个第三方包」。打开时仍然往 stderr 出声、往 trace 记一条 `collection_errors_allowed`

### 连续失败熔断

- **挡什么**：环境坏了 / prompt 崩了 / 今天这个模型不行 —— 继续跑只是匀速烧钱
- **阈值**：`consecutive_failure_limit`，默认 **3**
- **为什么比预算上限更早生效**：它把「钱花完了」变成「出问题了，去看 trace」，后者信息量大得多，省下的额度还能流给真有希望的 failure

### 越界计数（不拦，只数）

`src/aifix/violations.py` 从事件流里数三类越界尝试：想改测试文件、想逃出工作区、原地打转被中止。它**不改变任何判定**，只进评测对比表的「越界尝试」一列 —— 量化的是「不同模型有多不听话」。

刻意不把「补丁打不上」算进来：那是模型能力问题而非越界，混进来这一列就失去意义了。

三条匹配串里有两条来自第三方依赖（`harness/sandbox/base.py` 的「路径逃逸」、`harness/loop/agent_loop.py` 的「检测到疑似循环」），而 `pyproject.toml` 没锁上界。上游改一次措辞，那两类统计就会**永久归零，不报错、不崩溃**。`tests/test_violations.py` 里有两个哨兵测试直接对着上游的真实产物断言，上游一改措辞就会红。

---

## 三层预算

**全局 → 单 failure → 单次 AgentLoop。** 动态分配而非固定切分：前面省下来的额度自动流给后面难的。固定切分会出现「最后一个 failure 明明有钱，却因为自己那份用完了而放弃」。

| 层 | 实现 | 默认 |
|---|---|---|
| 全局 | `RunBudget`（`src/aifix/budget.py`） | `budget_tokens=500_000` / `budget_usd=2.0` / `budget_wall_seconds=1800.0` |
| 单 failure | `for_failure()` / `usd_for_failure()` | 剩余额度 ÷ 剩余 failure 数 |
| 单次 AgentLoop | `BudgetTracker` + `consume(cost_cap=...)` | detect 侧 `detector_max_tokens=20_000`；fix 侧取该 failure 的剩余 |

token 那边有下限 `FLOOR_TOKENS = 10_000`（再紧也要给一次有意义尝试的余地）。**美元那边刻意没有下限** —— 额度耗尽时若还给一个下限，闸就失效了，而「额度耗尽还在花」正是这个设计要挡住的事。

墙钟预算的**归属**与另外两个不同，这一点在评测里是决定性的：token 与美元是**模型**的属性（同一批任务、同一个上限，谁先烧完谁差，可比）；墙钟是**评测调度器**的属性（`--parallel 8` 时八个任务抢 CPU 跑全量 pytest，墙钟耗尽的概率远高于 `--parallel 1`）。把墙钟耗尽记成模型的失败，等于只改并行度就能改变修复成功率。所以 `RunBudget.exhaustion()` 返回 `(种类, 原因)` 而不只是消息，种类取值 `tokens` / `usd` / `wall`。

### 成本闸的契约，逐字

> **越线之后不再发起新的模型调用。**

**不是**「绝不超支」。成本只有在调用返回后才知道（`ModelUsage` 到达的那一刻），所以越线时**那一次调用必然已经花掉**。

因此超支上界是可陈述的：

| 场景 | 超支上界 |
|---|---|
| 单次 `aifix run` | 一次模型调用 |
| `aifix eval --budget-total` 整批 | **并发数 × 一次模型调用的成本**（不随任务数线性放大） |

整批那个上界靠「派发前预留」实现：算出 `cap` 的同一把锁内立即把 `cap` 记进 `spent`，任务跑完后再回填差额。若只在任务跑完后才累加，`parallel=N` 时 N 个并发槽位会在派发前全部读到同一个旧 `spent` —— 实测 `total_usd=1.0`、每任务花 `1.0`、4 个任务：`parallel=1` 正确地只花 $1.0，`parallel=4` 花掉 $4.0，**4 倍超支**，而 `parallel=4` 正是 `aifix eval` 的默认并发度。

这个上界有一个前提：**任务要么正常跑完、要么在没花钱之前就炸**。若某个任务是花过钱之后才抛，那笔钱在 `spent` 里就消失了，后续任务会拿着「以为还剩」的额度继续派发。这一条已知、未收紧，写在 `eval/runner.run_suite` 的 docstring 里。

### `detect` 不受美元闸约束

这是计划登记过的**有意偏差**：`detect_node` 只有 token 闸（`detector_max_tokens`），`consume()` 不传 `cost_cap`。

但它花掉的钱必须**在 detect 返回后立刻结算**，否则给 fix 算出来的额度是按「detect 还没花钱」算的，fix 会在可能已经越线的状态下发起新调用 —— 实测 `budget_usd=20`、单次调用 $15 时，单个 failure 会花掉 $45，**超支 1.67 次调用而不是一次**。`cli.run_once` 里 detect 与 fix 之间那一行 `budget.charge(...)` 就是为这件事存在的。

### 没有价格表时拒绝启动

显式设了美元上限却没配 `AIFIX_PRICE_MAP` 时，**当场报错，不启动**：

```console
$ AIFIX_PRICE_MAP='{}' aifix run . --budget 2
拒绝启动：设置了美元预算上限，但没有配置价格表，这个上限不会生效。
  没有 price_map 时成本恒为 0，闸永远不触发 —— 与其给一个假的保证，不如现在就停。
  修法一：配置价格表（每千 token 的 [输入价, 输出价]）
    export AIFIX_PRICE_MAP='{"deepseek-v4-pro": [0.003, 0.006]}'
  修法二：去掉美元上限，改用 AIFIX_BUDGET_TOKENS 限制 token
$ echo $?
1
```

「显式」由 pydantic 的 `model_fields_set` 判定，默认值不在其中。一处判定管住环境变量、构造参数、命令行三条来源（`--budget` 走 `model_copy(update=...)`，同样会被记住）。

价格表本身也在加载时校验格式：必须是扁平表 `{模型名: [输入价/1k, 输出价/1k]}`，**不是**分档表。分档表传进来时框架的 `cost_usd` 会在解包处抛 `ValueError` —— 而那已经是跑到一半、token 花掉之后了。成本计算是装饰性的，不该有崩掉整个 run 的权力，所以把它拦在启动阶段。

---

## 守卫本身也需要被攻击性地测试

「不许改测试文件」这道守卫**被绕过过两次**。两次都不是崩溃，都是静默放行。

### 一、伪造的 diff 前缀

守卫曾经只认 `a/` 与 `b/` 两种前缀，而 `git apply` 默认的 `-p1` 剥掉的是**任意**第一段。

喂进 `--- x/tests/test_add.py`：守卫读到的首段是 `x`（不在 `test_dirs` 里，放行），git 剥掉 `x/` 之后写的却是 `tests/test_add.py`。**断言被删掉，工具还回「补丁已应用」。**

修法是让守卫按 git 的真实规则剥前缀（`patch.py` 的 `_strip_p1`）：有 `/` 就剥掉第一段，没有就原样保留。「没有 `/` 就原样」不是补丁而是 git 的真实行为（`apply.c` 的 `stripath`：分段不够就停下）—— `diff.noprefix` 风格的 `--- calc.py` 会被写进 `calc.py`，无条件丢掉第一段会得到空路径，守卫和路径围栏都拿它没辙。

守卫现在**两条路径都查**：diff 头上的原样路径与 git 实际写入的路径，任何一条像测试文件就拒绝。宁可多拦不可漏放。

### 二、大小写不敏感的文件系统

macOS 与 Windows 的文件系统默认不区分大小写。`a/TESTS/test_add.py` 在一个大小写敏感的判定里不是 `tests` 目录，git 却老老实实把它写进了 `tests/test_add.py` —— 断言被删掉而守卫一声不吭。

`signals.under_dirs` 现在按 `casefold()` 比较。代价是一个同时存在 `tests/` 与 `TESTS/` 两个**不同**目录的仓库会被多拦一次；这道守卫挡的是「模型删断言让测试变绿」，宁可多拦不可漏放，何况那样的仓库本身就是病态的。

### 覆盖

两个变体都有专门的测试：`tests/test_tool_patch.py::test_forged_path_prefix_cannot_bypass_the_test_guard`、`::test_uppercase_test_dir_cannot_bypass_the_test_guard`，以及 `::test_single_segment_path_is_not_stripped`（钉住「没有 `/` 就不剥」这条 git 行为）。`tests/test_signals.py` 里另有一条区分度断言：`under_dirs("TESTDATA/x.py", ["tests"])` 必须为假 —— 不敏感只放宽大小写，不放宽分段边界。

**这两处的教训是同一条：守卫不能只在「正常输入」上测。** 一道守卫的价值全部体现在被攻击的时候，而它的失效形态是静默的 —— 不崩溃、不报错、测试全绿，只有承诺是假的。

`under_dirs` 与 `same_file` 这两份判定各自**只有一份实现**，同时服务守卫与评测（`tools/patch.py` ↔ `eval/mine.split_paths`；`eval/runner.locate_hit` ↔ `signals.files_outside_suspect`）。复制出来的两份会各自漂移 —— 本分支上就发生过一次：`mine` 已经升级成分段前缀匹配，`patch.py` 还停在 `parts[0] in test_dirs`，而 Maven 的 `test_dirs` 是 `["src/test"]`，首段是 `src`，守卫直接放行。

---

## 已知的局限

- **静态信号挡不住「在测试覆盖范围内把实现改成特例硬编码」。** 那需要覆盖率差分甚至语义分析。这不是一个能靠加守卫彻底解决的问题 —— 它是**目标项目测试覆盖率作为系统天花板**的直接后果。实证案例见[评测](evaluation.md)的「规格套利」一节。
- **配置项拼错不会报错。** `AifixConfig` 的 `model_config` 用 `extra="ignore"`，这是有意的 —— 它读的是进程环境，而进程环境不归它管：改成 `extra="forbid"`，上游镜像 / CI runner / 容器基座往里塞一个 `AIFIX_` 开头的变量就会让所有人启动失败。代价是 `AIFIX_MAX_ATTEMTPS=5` 这类拼写错误被静默吸收，看起来设上了，实际用的是默认值 —— 而报告里**没有**印出生效配置，所以这件事目前没有事后自查的办法，只能在设的时候拼对。同样的道理：一个配置项被删掉之后，对应的环境变量仍然设得上、仍然什么都不做（`AIFIX_ALLOW_TEST_EDITS` 就是这样一个已删字段）。
- **可编辑安装能让验证悄悄失效，aifix 只出声、不解决。** 目标项目若把自己 `pip install -e .` 进了测试解释器，site-packages 里会留一条指向**源仓库**的路径记录，于是 `import <目标包>` 可能解析到源仓库那份**没打补丁**的代码 —— 测试照跑照绿，而 `verify`（系统里唯一有资格说「修好了」的地方）验的是原代码。这是这个项目最怕的那类失效：不崩溃、不报错，只有结论是假的。`baseline` 之前会跑一次 `imports_outside_worktree()` 并往 stderr 出声，但**它是近似**：它复现不了 `conftest.py` 里手写的 `sys.path` 改动、`--import-mode=importlib` 的细节和 rootdir 之外的插件，**返回空不等于安全**。不把它升级成拦截，是因为一个会误报的信号如果有权中止整个 run，用户为了跑起来就会去关掉它 —— 那比没有更糟。真正的自保是在目标项目的 pytest 配置里设 `pythonpath`。理由与实测见[适配器](adapters.md#换来的真实风险可编辑安装会让验证悄悄失效)。
- **「收集错误」这道闸分不出「机器缺依赖」和「仓库自己的导入 bug」。** 判据只看文件级 error 的条数与占比，不看报错内容。几个测试文件一起 import 不到同一个**仓库自己的**模块（改名、忘了提交一个文件）时，那是个真 bug、值得修，却会被判成环境故障当场中止。放宽阈值不解决问题，只是把误判换个方向 —— 真要分得开，得去读 `ModuleNotFoundError` 里的模块名并判断它是不是这个仓库提供的，那是另一件事（且对 Maven 侧无解）。所以留了 `AIFIX_ALLOW_COLLECTION_ERRORS=1` 这个逃生口，并在中止消息里直接写出来：一道会误判的闸如果没有出路，用户唯一的选择是去改 aifix 的源码。
- **成本闸中止时的清理不完整。** `consume()` 越线后 `aclose()` 只关掉了框架 `AgentLoop.run()` 的外层壳，真正持有 `ExitStack`（还原「打转纠偏」时调高的采样温度）和三个 OpenTelemetry span 的 `_run_from` 挂在原地，要等事件循环回收异步生成器才被终结。**升温泄漏给下一次调用的隐患仍然完整存在**，还原时机不确定。跑成本闸测试时打出来的 `Failed to detach context` 堆栈就是这件事的收据 —— 没有给它装 filter 消音，因为那等于撕掉收据、隐患照旧。真修需要给框架的 `run()` / `resume()` 各包一层 `contextlib.aclosing`。细节写在 `src/aifix/agents/runner.py`。
