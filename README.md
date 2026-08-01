# aifix

**失败的测试进去，一条经过验证的补丁分支出来。**

```
$ aifix run .
# aifix run 230356cb

- 适配器：pytest
- 分支：`aifix/230356cb`
- 修复：**2 / 2**
- 成本：$0.02（27,480 tokens）

| 测试用例 | 结果 | 尝试次数 | 中止原因 |
|---|---|---|---|
| `tests/test_calc.py::test_add` | 已修复 | 1 | — |
| `tests/test_calc.py::test_div` | 已修复 | 1 | — |

合并：`git merge aifix/230356cb`
```

主工作区一个字节没被碰过。改动全在一条独立分支上，你自己决定合不合。

> 上面这段是**真跑出来的**，不是手写的示意。但它用的是替身模型（补丁由脚本给定），所以成本那一行是替身的用量，不代表真实模型的经济性——真实模型的一次实测在 M1 验收里：2 / 2 修复，**$0.29 / 92,682 tokens**。这个 README 里出现的每一段输出都遵守同一条规矩：**要么是真跑出来的，要么明说它不是。**

---

## 这个项目在赌什么

一句话：**只有零 LLM 的确定性代码有资格说「修好了」。**

模型会说自己修好了。它说的不算。判定由 `verify` 节点做——跑全量测试、和 baseline 比失败集、三态判定（BETTER / SAME / WORSE），全程不调用任何模型。模型负责**生成**，harness 负责**判定、约束、记账**。

这条主张听起来平淡，但它决定了这个项目里几乎每一个设计：

- 测试是 oracle，所以**绝不允许 agent 改测试文件**——那等于让它改判卷标准
- 判定说 BETTER 才 commit，说别的就 rollback，没有中间地带
- 一个字节都没改却判 BETTER，降级成 SAME（那说明目标用例本来就是抖的）
- 补丁被自己抵消、`git add` 什么都没暂存，同样降级——**报告说「已修复」时，分支上必须真的有东西**

## 快速开始

需要 Python ≥ 3.11 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/sumengnan/ai-fix-code-loop
cd ai-fix-code-loop
uv sync
```

先在一个有失败测试的 git 仓库上空跑一次，看看有多少活——**不花一分钱，不调用任何模型**：

```bash
uv run aifix run /path/to/your/repo --dry-run
```

真跑需要配模型。两条路由分开配（诊断和修复可以用不同的模型）：

```bash
export AIFIX_FIXER__MODEL=deepseek-v4-pro
export AIFIX_FIXER__BASE_URL=https://your-endpoint/v1
export AIFIX_FIXER__API_KEY=sk-...
export AIFIX_DETECTOR__MODEL=deepseek-v4-flash    # 诊断可以用便宜的
export AIFIX_DETECTOR__BASE_URL=https://your-endpoint/v1
export AIFIX_DETECTOR__API_KEY=sk-...

# 想用美元预算就必须配价格表，否则启动时会被拒绝（见下）
export AIFIX_PRICE_MAP='{"deepseek-v4-pro": [0.003, 0.006], "deepseek-v4-flash": [0.0002, 0.0004]}'

uv run aifix run /path/to/your/repo --budget 0.50
```

**前提**：目标仓库是 git 仓库、已跟踪文件没有未提交改动（baseline 从 HEAD 算，工作区另有改动的话算出来的失败集和你眼前看到的对不上）。

## 它为什么敢跑在你自己的仓库上

四层封闭，每一层都是独立的：

| 层 | 做什么 |
|---|---|
| **能力** | 工具面是**白名单**——`read_file` / `read_symbol` / `list_files` / `grep` / `edit_file` / `apply_patch` / `run_tests`，外加条件注册的 `ask_user`。**没有 `run_shell`**。不是「禁掉危险的」，是「只放行列举过的」 |
| **路径** | 每个路径经 `resolve_in_workspace` 规约，`../`、绝对路径、符号链接逃逸一律拒绝 |
| **进程** | 测试进程的 cwd 锁在 worktree 里 |
| **git** | 改动发生在 `.aifix/runs/<run_id>/tree` 的独立 worktree，交付物是一条分支。主工作区不可达 |

加上八道守卫（改测试文件、空 diff、巨型 diff、守卫连撞放弃、回归回滚、flaky 过滤、baseline 全是收集错误、连续失败熔断）和三层预算。

**成本闸的契约要说清楚**：是「**越线之后不再发起新的模型调用**」，**不是**「绝不超支」。成本只有在调用返回后才知道，所以最后那一次必然已经花掉。超支上界是可推导的，写在 `--help` 里。

细节见 [安全边界](docs/safety.md)。

## 它怎么证明自己有用

一个自修复系统最容易骗人的地方是**它说自己修好了**。所以这个项目自带一整套评测：

```bash
uv run aifix mine . --limit 60 --max-tasks 12 --out evals/tasks.jsonl        # 从 git history 挖任务集
uv run aifix eval evals/tasks.jsonl --label deepseek-v4-flash \
        --budget-per-task 0.60 --budget-total 6.00                           # 跑一轮（会花钱）
uv run aifix eval-report evals/results-*.jsonl                               # 出对比表
```

`mine` 找的是**把测试从红修到绿的真实 commit**——ground truth 自带，不需要人标注，分布也不跑偏。产出的每个任务都经过四阶段验证才入选。

对比表长这样。**全部是真跑出来的**——39 个任务挖自**两个**仓库的 git history（[ai-harness-framework](https://github.com/sumengnan/ai-harness-framework) 14 个 + ai-learning-helper 25 个），明细在 [`evals/`](evals/)：

```
| 模型 | 来源 | 任务数 | 定位准确率（分数, 95%CI） | 修复成功率（分数, 95%CI） | 平均成本 | 平均 tokens | 越界 | 可疑信号 | 评测故障 |
|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4-flash         | mined | 37 | 51% (19/37, CI 36%–67%) | 27% (10/37, CI 15%–43%) | $0.4316 | 423,693 | 0 | 0 | 2 |
| qwen3-coder-flash         | mined | 39 | 90% (35/39, CI 76%–96%) | 18% ( 7/39, CI  9%–33%) | $0.2345 | 454,424 | 0 | 1 | 0 |
| qwen3-coder-flash-after   | mined | 39 | 92% (36/39, CI 80%–97%) | 74% (29/39, CI 59%–85%) | $0.1311 | 249,501 | 0 | 2 | 0 |
| qwen3-coder-flash-recount | mined | 39 | 90% (35/39, CI 76%–96%) | 79% (31/39, CI 64%–89%) | $0.1241 | 238,070 | 0 | 5 | 0 |
```

### 后三行是一次干净的对照：18% → 74%

同一个模型、同一批 39 个任务、同一份预算，**只换了工具面**。四个改动：

1. **`git apply --recount`** —— 不信 `@@` 里的行数，从正文推
2. **`edit_file`** —— 给原文与新文，不写 diff、不数行号
3. **`read_symbol`** —— 按名字读完整定义，Python 走 `ast`
4. **Detector 看得见候选位置的真实源码**（零 LLM，不多花一个回合）

**22 个任务从失败翻成成功，0 个回归**，两个置信区间完全不重叠。tokens 降 45%、成本降 44% —— 修得更多反而更便宜，因为省掉的正是「在死循环里空转」的那部分：token 耗尽的任务从 **27/39** 降到 **9/39**。

起因是基线明细里的一组读数：`apply_patch` 被调 332 次、**失败 309 次（93%）**，而能解析的坏补丁里 **247/247 是 `@@ -a,b +c,d @@` 的行数与正文对不上——100%**。模型的正文往往是对的，它栽在记账上；而数数正是 LLM 结构性最弱的能力。

第四行是**口径确认**：74% 是用一份手写的表头重算量出来的，后来换成了 git 自己的 `--recount`。两轮区间几乎完全重叠（74% vs 79%），逐任务只有 4 个翻转且**方向一进三出** —— 结论是「换实现没让它掉下去」，**不是「`--recount` 更好」**。39 个任务分不出这 5 个百分点。

按仓库拆开（第三轮）：

| 仓库 | 任务 | 修复成功率 | 定位准确率 |
|---|---|---|---|
| ai-harness-framework | 14 | **79%**（11/14，旧工具面 21%） | 100% (14/14) |
| ai-learning-helper | 25 | **80%**（20/25，旧工具面 16%） | 84% (21/25) |

**定位从来不是瓶颈**（90%），卡住的一直是「改不对」——而那 90% 与 18% 之间的落差，几乎全部来自一个可以用确定性代码消灭的机械故障。

### 这套评测抓住的第一个人，是它的作者

第一轮我只跑了 12 个任务，得到 `定位 25% / 修复 58%`，并在这份 README 里写下过一条结论：「诊断多半是错的，补丁多半是对的，detect 这一步可能不值它的钱」。

第二轮在**同一个仓库**上跑 14 个任务：`定位 43% / 修复 43%`。

两轮的区间大幅重叠，每一轮的点估计都落在对方的区间里——**统计上无法区分，那条结论是从噪声里读出来的。** 区间就印在表里，是我没照着它读。

这条更正留着不删。一个专门用来判定别人对不对的系统，最该能判定的是自己。

**第二次，抓的还是同一个人。** deepseek 那轮里我写过一句：「换到更大的代码库上，修复率掉了一半以上（43% vs 17%），这是这轮里最像真信号的一条」，并据此判断**瓶颈是仓库规模**。

换掉工具面之后，同一批任务：

| 仓库 | 旧工具面 | 新工具面 |
|---|---|---|
| ai-harness-framework（小） | 21% (3/14) | **79%** (11/14) |
| ai-learning-helper（大） | 16% (4/25) | **80%** (20/25) |

**落差没了。** 79% 对 80% —— 大仓库不再更难。

**规模从来不是障碍本身，它只是放大了补丁接口的缺陷**：文件越长，上下文行越容易抄错、表头行数越容易算错，而那正是 93% 的补丁失败原因。换掉那个接口，两个仓库就站到了同一条线上。

这次的错法和上次不同。上次是**从噪声里读出结论**（区间重叠却当成信号）；这次相关性是真的、区间也是真的分开的——**错在归因**。我把「大仓库更难」直接读成了「难在规模」，而真正起作用的变量藏在两者之下。数据没骗人，是我少问了一层。

一条统计学上的自警：这仍然是**观察到的相关性被一次干预推翻**，而干预只做了一次、n=39。别把「落差消失」再读成新的因果定论——它只说明旧那条因果说不通了。

（有一条观察仍然成立，因为它是**直接测量**而不是统计推断：纯断言失败的 traceback 里根本没有产品代码的栈帧，`locate_source` 实测返回 0 个候选，Detector 在那些任务上确实在盲猜。但"盲猜"和"不值它的钱"是两件事——后者要一个关掉 detect 的对照实验，没做。）

这张表里另外几个刻意的设计：

- **比率后面跟着分数和 95% 置信区间。** 用 Wilson 而不是 Wald——`1/1` 在 Wald 下给出 `[100%, 100%]`，一个宣称"确定无疑"的区间，而"只跑了一个任务"恰恰是这张表最需要说出口的事
- **来源不混算。** 挖掘（真实分布）与人造变异（`aifix mutate`，冒烟用）分布不同，成功率平均成一个数字是错的，所以它们各占一行
- **没配价格表时，成本那格写的是「未知」而不是 `$0.0000`。** 一列整齐的 `$0.0000` 会被读成"极其便宜"，而真相是"这一列没数据"。这个项目为「假的 `$0.00`」栽过三次
- **「可疑信号」列**（删除公开符号 / 新增模块级可变状态 / 改动落在诊断嫌疑文件之外）：纯静态、零模型调用、**只标注不改判定**。修复成功率高、可疑信号也高——那是**规格套利**的指纹。四轮里越界尝试全是 0，一次都没想改测试文件。可疑信号最多的一轮是 5 个，查到 fact 层面全部是 `files_outside_suspect`（补丁落在诊断点名的文件之外），其中多数同时**定位未命中**——那是**诊断指错了文件、模型改对了地方**，不是规格套利

细节见 [评测](docs/evaluation.md)。

## 一个真实的失败案例

M3 的真实模型验收里，探针任务的断言是 `add(1,1)==2 and add(1,1)==3`——逻辑上不可能。模型把 `add` 改成有状态函数满足了它，顺手删掉了无测试覆盖的 `mul`。系统报告「修复 1/1」并给出 merge 命令。

**每一道守卫都正常工作了。** 它们检查的都是 agent 的**行为**（改没改测试、diff 大不大、越没越界），没有一道检查补丁的**合理性**；而三态判定只能看见测试覆盖到的东西。

这是「目标项目的测试覆盖率即系统天花板」的实证。「可疑信号」那一列就是这次之后加的——它不解决问题（静态信号挡不住"在覆盖范围内硬编码特例"），但它让**人**在合并之前有东西可看。规格里写死了：**不做 Reviewer agent，审查者是人**。

## 出了问题怎么查

跑的时候屏幕上就有东西看 —— 进度走 stderr（报告走 stdout，`aifix run . > report.md` 存出来的文件顶上不会粘着进度）。模型每调一次工具印一行，**成功绿勾、失败红叉**，后面跟着它对什么做了这件事、结果如何：

```
      ✓ 第 5 步 read_file  src/shopcart/cart.py → 56 行
      ✗ 第 6 步 apply_patch  src/shopcart/cart.py → 补丁无法应用（git apply --check 失败）：error: corrupt patch at line 10
      ✓ 第 7 步 run_tests  test_排行_按单品总价降序 → 1 passed in 0.04s
```

失败那行**不截断**（放不下就换行接着写）—— 一次实测里被省略号吃掉的恰好是 `corrupt patch at line 10`，也就是那次失败的全部信息量。

跑完之后，每次 run 都在 `.aifix/runs/<run_id>/` 落三份东西：`events.jsonl`（模型每一步看到什么、决定做什么）、`facts.jsonl`（领域判断的结论）、`report.md`。

```bash
uv run aifix replay <run_id>            # 逐步复盘；--step N 只看某一步，--full 不截断
uv run aifix ingest                     # 把各次 run 的事实灌进 .aifix/trajectory.db（幂等）
uv run aifix stats                      # 跨 run 汇总：适配器、守卫触发、可疑信号
```

细节见 [诊断](docs/diagnostics.md)。

## 从一个 issue 开始

上面那条链路的入口是「已经红了的测试」。**M6 把入口往前挪了一格**：输入变成一段人话。

```
你在自己的 issue 里评论一句 /aifix
  → Actions 起来
  → 读 issue → 写一条复现测试 → 跑一遍，必须红
  → detect → fix → verify（现有核心循环一行没改）
  → 开 PR：复现测试 + 补丁 + 报告
  → 你审 PR                      ← 唯一一道人闸
```

复现测试先 commit 进 HEAD，随后 worktree 从 HEAD 建出来，baseline 自然把它认成一个失败用例 —— 所以核心循环完全不知道自己在被 issue 驱动。

### 它卡住时会问你，而不是猜

有一类问题读多少代码都推不出来：**「购物车为空时该返回 None 还是抛异常」是产品决策，不是实现细节。** 遇到这种，agent 会停下来问，并给出编号选项：

```
## 需要你回答一个问题

空购物车时 most_expensive() 应该：
  1. 返回 None（当前行为，但调用方没判空 → 就是这个崩溃）
  2. 抛 ValueError

回复 /aifix 1 继续。（命令行是 aifix answer 1）
```

**几种改法都能让测试变绿时它不该问** —— 那种情况自己试，由 verify 判对错。判定权在测试那里，不在人那里。这条线由代码判死：一次 run 只能问一个，问之前必须先读过代码，而且必须给 2-4 个选项（自由回复要再过一次模型解析意图，那一步出错的方式是「按你没说过的意图改了代码」）。

答复之后是**重新跑一遍**，不是从断点继续 —— Actions 的 job 一次性，那条路上没有断点可恢复。两条入口用同一套语义，才不会各错各的。

**三条交付通路**，取舍写在 [`docs/superpowers/plans/2026-07-29-m6-issue-driven.md`](docs/superpowers/plans/2026-07-29-m6-issue-driven.md) 里：

| 情形 | 产出 |
|---|---|
| 写不出复现测试 | 只回帖，列出 issue 缺哪些信息。不建分支、不开 PR |
| 写出了复现、没修好 | **照样开 PR**，标题标明「未修复」—— 一条红着的复现测试本身就是产出 |
| 修好了 | 开 PR，报告写进正文 |

想先自己量一量模型写复现测试的本事，不必碰 GitHub：

```bash
uv run aifix reproduce . --issue-text bug.md     # 首行当标题，其余当正文
```

拿历史上真实的修复 commit message 直接喂进去就行 —— 它本来就是这个形状。跑完不在你仓库里留东西。

**触发条件是两条同时成立**：评论者是仓库所有者，**且 issue 也是他自己提的**。第二条不是权限洁癖：issue 正文会作为输入交给模型，而外部提交的正文是不可信文本。只限制触发者挡不住注入 —— 外人提一个藏了指令的 issue，等仓库主觉得该修、顺手打上 `/aifix`，就绕过去了。

**已知的空白，说准确点**：本地那条端到端是真跑的（`tests/test_issue_e2e.py`）——真实形状的 event 载荷、真的 reproducer 落盘、真跑 pytest 的红检、真的 `run_once`、真的 push 到一个 bare 远端，最后断言交付分支上确有两个提交且 `calc.py` 被改对了。**只有模型和 GitHub 是替身。**

盖不住的因此只剩两件：`gh` 的命令真被 GitHub 接受（需要真账号），以及**真实模型读一段人话能不能写出对的复现测试**——后者是这条路的天花板，而它一个数字都还没有。想量它不必碰 GitHub，用上面那个 `aifix reproduce` 就行。

## 支持哪些语言

`pytest` 与 `Maven`（surefire）。加一门新语言要实现 `ProjectAdapter` 的 11 个方法。

`MavenAdapter` 是**第二个**实现，而它存在的理由不只是支持 Java——**一个只有单一实现的接口，无法区分「抽象对」和「抽象恰好长得像那一个实现」。**

它撞出了**六处裂缝**，全部修掉。其中五处的症状是同一种：**静默产出 0 个任务，不报任何错**。挖掘链路上每一步的失败模式都是「筛掉」，而筛空与「这个仓库最近没有红转绿的提交」长得一模一样。

那六处是什么、怎么找出来的，见 [适配器](docs/adapters.md)。

## 文档

| | |
|---|---|
| [架构](docs/architecture.md) | 一次 run 是怎么转的：六个节点、两层状态、worktree 交付 |
| [安全边界](docs/safety.md) | 四层封闭、八道守卫、三层预算、成本闸的确切契约 |
| [评测](docs/evaluation.md) | `mine` / `mutate` / `eval`、双档打分、Wilson 区间、可疑信号 |
| [诊断](docs/diagnostics.md) | 跑的时候看得见什么、退出码、trace 布局、`replay`、SQLite 跨 run 轨迹 |
| [适配器](docs/adapters.md) | 协议逐个成员、写新适配器的清单、六处裂缝的完整记录 |
| [M6 计划](docs/superpowers/plans/2026-07-29-m6-issue-driven.md) | issue 驱动：七条设计决策、为什么只有一道人闸、Actions 的坑 |

设计规格与实现计划在 [`docs/superpowers/`](docs/superpowers/) 下——每个里程碑一份规格、一份计划，包括那些被证伪之后留痕更正的地方。

## 命令一览

```
aifix run <repo>            修复失败的测试            --test / --budget / --dry-run / --quiet
aifix answer <编号> [repo]   回答上次 run 提的问题      --run-id / --budget / --quiet
aifix reproduce <repo>      把缺陷报告译成复现测试     --issue-text / --title / --keep
aifix issue handle          处理一次 issue_comment 事件 --repo / --event
aifix mine <repo>           从 git history 挖任务集    --limit / --max-tasks / --out
aifix mutate <repo>         人造变异生成冒烟任务集      --max-tasks / --max-new-failures / --scope / --seed / --out
aifix eval <tasks.jsonl>    在任务集上跑评测           --parallel / --label / --out / --budget-per-task / --budget-total
aifix eval-report <...>     把若干轮结果渲染成对比表
aifix replay <run_id>       回放一次 run 的逐步复盘     --repo / --step / --full
aifix ingest                把各次 run 的事实灌进 SQLite --repo / --runs-dir
aifix stats                 跨 run 汇总                 --repo
```

每个子命令的 `--help` 都写了取舍，不只是参数说明。

**退出码**：`aifix run` 只在这次 run **没跑成**时退 1（崩溃、baseline 全是收集错误、模型端点不通、preflight 拒绝）。「修好了 0 个」和「预算耗尽」都退 **0** —— 那是正常收场，结论仍然可信。判据只有一份名单（`cli._FAILED_RUN_KINDS`），漏登记一种的后果是那一类静默退 0、流水线把它读成成功；preflight 就这么漏过一整轮，见[诊断](docs/diagnostics.md#退出码这次-run-到底跑成没有)。

## 配置

环境变量前缀 `AIFIX_`，嵌套用 `__`（`AIFIX_FIXER__MODEL`）。常用的几个：

| | 默认 | |
|---|---|---|
| `AIFIX_BUDGET_USD` | `2.0` | 需要 `AIFIX_PRICE_MAP`，否则显式设置时**启动即拒绝** |
| `AIFIX_BUDGET_TOKENS` | `500000` | |
| `AIFIX_BUDGET_WALL_SECONDS` | `1800.0` | |
| `AIFIX_MAX_ATTEMPTS` | `3` | 每个 failure 最多试几轮 |
| `AIFIX_MAX_DIFF_LINES` | `300` | 超过即判为整文件重写 |
| `AIFIX_ASK_USER` | `true` | 信息不全时允不允许停下来问人。**没人能回答的场合要关掉** —— `aifix eval` 已经强制关了 |
| `AIFIX_TEST_TIMEOUT_SECONDS` | `1800.0` | 跑一次**全量**的超时。套件本身就超过这个数的项目必须调大，否则每一轮 verify 都被杀在半路 |
| `AIFIX_SCOPED_TEST_TIMEOUT_SECONDS` | `600.0` | 只跑几个用例时的超时（`run_tests` 与 flaky 复跑） |
| `AIFIX_CONSECUTIVE_FAILURE_LIMIT` | `3` | 连着几个没修好就中止整个 run |
| `AIFIX_PRICE_MAP` | `{}` | `{"模型名": [输入价/1k, 输出价/1k]}` |
| `AIFIX_TEST_PYTHON` | 自动探测 | 跑**目标项目**测试用的解释器。不配就找源仓库的 `.venv/bin/python` / `venv/bin/python`，再没有才退回 aifix 自己的解释器 |
| `AIFIX_ALLOW_COLLECTION_ERRORS` | `false` | 允许「baseline 里全是测试文件收集失败」的仓库照常开修。默认关，见下 |

**为什么需要 `AIFIX_TEST_PYTHON`**：目标项目的测试依赖装在**它自己**的环境里。写死 aifix 的解释器等于要求你把别人的依赖装进 aifix 的 venv —— 实测拿 aifix 的 venv 去跑 `ai-harness-framework`：11 个 collection error，一个用例都没跑到；换它自己的 `.venv`：673 passed。配了一个不可执行的路径时 **preflight 当场拒绝启动**，不会拖到 baseline 才以「测试没跑成」的面目出现。

**用错解释器时会当场停下**：那 11 个 collection error 不是空气 —— pytest 收集中断时照样写出一份完整的 JUnit 报告，里面是一条条文件级 `<error>`，它们会被翻译成可重跑的 node id 排进队列。aifix 在 baseline 之后查一次占比（文件级 id 条数 ≥ 2 且严格过半即中止），中止消息里写明这不是模型的问题并指向 `AIFIX_TEST_PYTHON`，退出码 1。判据与绕过办法（`AIFIX_ALLOW_COLLECTION_ERRORS`）见[安全边界](docs/safety.md#baseline-全是收集错误collect-中止)。

**配它就要知道一个陷阱**：目标项目若把自己可编辑安装（`pip install -e .`）进了那个解释器，`import <目标包>` 可能解析到**源仓库**而不是 worktree 里那份打了补丁的代码 —— 测试照跑照绿，验证的却是没打补丁的代码。aifix 在 baseline 之前做一次近似探测并往 stderr 出声，但那是提醒不是保证。可靠的自保是在目标项目的 pytest 配置里设 `pythonpath`（如 `[tool.pytest.ini_options] pythonpath = ["src"]`）。细节见 [适配器文档](docs/adapters.md#用哪个解释器跑-pytest)。

**为什么没配价格表就拒绝启动**：不配价格表时成本恒为 0，美元闸永远不会触发。你设了上限，系统欣然接受，然后一分钱不拦。与其给一个假的保证，不如现在就停。

完整列表见 `src/aifix/config.py`——每个字段的注释都写了它为什么存在、默认值是怎么定的。

## 项目状态

- **925 个测试**，全绿（2026-08-01 实测，`-n 8` 并行 **145 秒**；同一天早些时候 906 项 `-n 8` **185 秒**、858 项串行 **993 秒**）。**这几个数之间还夹着一个被我读错的 692 秒**：那次量的时候后台正跑着一轮 3 并发的评测，而评测的每个任务本身就在 spawn 完整的测试套件 —— 12 核被两边分掉，我却把它当成「并行只快 30%」写了进来。机器不空时量出来的不是并发度，是争抢。给多个读数而不是一个「权威」数字这条规矩，正是为这种事立的。本机装了 `mvn`，Maven 那批是真跑的
- 依赖 [ai-harness-framework](https://github.com/sumengnan/ai-harness-framework)（同作者，提供 AgentLoop / 沙箱 / 预算 / 遥测）
- 第一阶段（M1 闭环 → M2 靠谱 → M3 可度量 → M3b 成本闸 → M4 有结论 → M5 跨语言与可诊断 → M6 issue 驱动）已完成

**还没做的**（都是有意留的，不是忘了）：覆盖率差分、SWE-bench Lite / Defects4J 的对外可比数字、第三个适配器、issue 驱动那条链路的**真实端到端验收**（需要真 runner 与 API key，见上）。

**做了一半的**：复现测试准确率的离线评测 —— 方法与 workflow 已经写好（`.github/workflows/aifix-repro-eval.yml`：checkout 到真实 `fix(...)` 提交的父提交，只喂 commit message，看复现测试红不红），但**一个数字都还没跑出来**。别把「有 workflow」读成「量过了」。

**已知限制**：目标项目把自己可编辑安装进测试解释器时，`import <目标包>` 可能解析到源仓库而不是打了补丁的 worktree —— aifix 只做一次**近似**探测并出声，不解决（解决它要么接管目标项目的安装方式，要么改写它的 `sys.path`）。那道探测复现不了 `conftest.py` 里手写的 `sys.path` 改动，**返回空不等于安全**。见 [适配器文档](docs/adapters.md#换来的真实风险可编辑安装会让验证悄悄失效)。

**明确不做**：Web UI、Reviewer agent、主动扫描驱动、自动 merge。

## 这个项目为什么长这样

它是一个 **agent harness 工程**的练习：围绕一个随机的模型，搭确定性的脚手架——循环控制、上下文管理、工具面与权限、状态与断点、验证、观测、预算闸。

开发过程中反复撞上同一种失效模式，撞了**十次以上**：

> **不崩溃、不报错、测试全绿，只有数字或承诺是假的。**

报告里的成本显示 `$0.00`（三个不同的文件各犯一次）；定位准确率量的其实是模型的路径书写风格；「修复 1/1」记的是一个压根没轮到的用例；一个精确措辞但从没验证过的预算上界实际超支 4 倍；`assert "0%" in "100%"` 恒为真；一个测试整体替换了被 patch 的函数，代码提前返回、断言恒真却一直是绿的；「不许改测试文件」的守卫能被一行伪造的 diff 头绕过，而新加的测试只覆盖老实路径，**给了它一个不配得的绿灯**。

所以这个仓库里有些看起来过度的东西：几乎每条断言都配着反向对照，注释里大量写「为什么不这么做」，实测数字带着日期，被证伪的判断在文档里留痕更正而不是悄悄删掉。**在一个专门用来判定别人对不对的系统里，自己说谎的代价格外高。**

---

*仓库尚未添加 LICENSE 文件。在补上之前，请按「保留所有权利」对待。*
