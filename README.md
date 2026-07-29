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
| **能力** | 工具面是**白名单**——读文件、grep、跑测试、打补丁。**没有 `run_shell`**。不是「禁掉危险的」，是「只放行列举过的」 |
| **路径** | 每个路径经 `resolve_in_workspace` 规约，`../`、绝对路径、符号链接逃逸一律拒绝 |
| **进程** | 测试进程的 cwd 锁在 worktree 里 |
| **git** | 改动发生在 `.aifix/runs/<run_id>/tree` 的独立 worktree，交付物是一条分支。主工作区不可达 |

加上七道守卫（空 diff、巨型 diff、改测试文件、回归回滚、flaky 过滤、连续失败熔断、守卫连撞放弃）和三层预算。

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

对比表长这样。**这是真跑出来的**——12 个任务挖自 [ai-harness-framework](https://github.com/sumengnan/ai-harness-framework) 的 git history，用 `deepseek-v4-flash` 真花钱跑了一轮（$4.54 / 445 万 tokens，明细在 [`evals/`](evals/)）：

```
| 模型 | 来源 | 任务数 | 定位准确率（分数, 95%CI） | 修复成功率（分数, 95%CI） | 平均成本 | 平均 tokens | 平均尝试 | 越界尝试 | 可疑信号 | 评测故障 |
|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4-flash | mined | 12 | 25% (3/12, 95%CI 9%–53%) | 58% (7/12, 95%CI 32%–81%) | $0.3782 | 370,969 | 1.2 | 0 | 0 | 0 |
```

**定位 25%，修复 58%——诊断多半是错的，补丁多半是对的。** 这正是双档分开打分的理由：合成一个数字就看不见这件事。

追下去发现原因不在模型：这批任务全是**纯断言失败**，而断言失败的 traceback 里**根本没有产品代码的栈帧**——被调函数正常返回了，栈上只有测试自己。实测 `locate_source` 在这些任务上返回 **0 个候选**。所以 Detector 是在**盲猜**：它只看得到测试源码和断言消息，于是猜出 `loop.py`、`checkpoint_store.py`、`llama_stack/run_state.py` 这类看着合理但并不存在的路径。而 Fixer 有 grep 和读文件的工具，自己把真文件找了出来。

**这条结论有产品含义**：对断言失败占多数的套件，detect 这一步可能不值它的钱。这就是评测该产出的东西——不是一个漂亮的百分比，而是一个能改设计的判断。

区间也在说话：`58% (7/12, 95%CI 32%–81%)` 意思是 12 个任务只够把真值圈在三分之一到五分之四之间。想收窄就得加任务，不能靠改措辞。

这张表里另外三个刻意的设计：

- **来源不混算。** 挖掘（真实分布）与人造变异（`aifix mutate`，冒烟用）分布不同，成功率平均成一个数字是错的，所以它们各占一行
- **没配价格表时，成本那格写的是「未知」而不是 `$0.0000`。** 一列整齐的 `$0.0000` 会被读成"极其便宜"，而真相是"这一列没数据"。这个项目为「假的 `$0.00`」栽过三次
- **「可疑信号」列**（删除公开符号 / 新增模块级可变状态 / 改动落在诊断嫌疑文件之外）：纯静态、零模型调用、**只标注不改判定**。修复成功率高、可疑信号也高——那是**规格套利**的指纹。这一轮是 0，说明 7 个修复没有一个走捷径

细节见 [评测](docs/evaluation.md)。

## 一个真实的失败案例

M3 的真实模型验收里，探针任务的断言是 `add(1,1)==2 and add(1,1)==3`——逻辑上不可能。模型把 `add` 改成有状态函数满足了它，顺手删掉了无测试覆盖的 `mul`。系统报告「修复 1/1」并给出 merge 命令。

**每一道守卫都正常工作了。** 它们检查的都是 agent 的**行为**（改没改测试、diff 大不大、越没越界），没有一道检查补丁的**合理性**；而三态判定只能看见测试覆盖到的东西。

这是「目标项目的测试覆盖率即系统天花板」的实证。「可疑信号」那一列就是这次之后加的——它不解决问题（静态信号挡不住"在覆盖范围内硬编码特例"），但它让**人**在合并之前有东西可看。规格里写死了：**不做 Reviewer agent，审查者是人**。

## 出了问题怎么查

每次 run 都在 `.aifix/runs/<run_id>/` 落三份东西：`events.jsonl`（模型每一步看到什么、决定做什么）、`facts.jsonl`（领域判断的结论）、`report.md`。

```bash
uv run aifix replay <run_id>            # 逐步复盘；--step N 只看某一步，--full 不截断
uv run aifix ingest                     # 把各次 run 的事实灌进 .aifix/trajectory.db（幂等）
uv run aifix stats                      # 跨 run 汇总：适配器、守卫触发、可疑信号
```

细节见 [诊断](docs/diagnostics.md)。

## 支持哪些语言

`pytest` 与 `Maven`（surefire）。加一门新语言要实现 `ProjectAdapter` 的 11 个方法。

`MavenAdapter` 是**第二个**实现，而它存在的理由不只是支持 Java——**一个只有单一实现的接口，无法区分「抽象对」和「抽象恰好长得像那一个实现」。**

它撞出了**六处裂缝**，全部修掉。其中五处的症状是同一种：**静默产出 0 个任务，不报任何错**。挖掘链路上每一步的失败模式都是「筛掉」，而筛空与「这个仓库最近没有红转绿的提交」长得一模一样。

那六处是什么、怎么找出来的，见 [适配器](docs/adapters.md)。

## 文档

| | |
|---|---|
| [架构](docs/architecture.md) | 一次 run 是怎么转的：六个节点、两层状态、worktree 交付 |
| [安全边界](docs/safety.md) | 四层封闭、七道守卫、三层预算、成本闸的确切契约 |
| [评测](docs/evaluation.md) | `mine` / `mutate` / `eval`、双档打分、Wilson 区间、可疑信号 |
| [诊断](docs/diagnostics.md) | trace 布局、`replay`、SQLite 跨 run 轨迹 |
| [适配器](docs/adapters.md) | 协议逐个成员、写新适配器的清单、六处裂缝的完整记录 |

设计规格与实现计划在 [`docs/superpowers/`](docs/superpowers/) 下——每个里程碑一份规格、一份计划，包括那些被证伪之后留痕更正的地方。

## 命令一览

```
aifix run <repo>            修复失败的测试            --test / --budget / --dry-run
aifix mine <repo>           从 git history 挖任务集    --limit / --max-tasks / --out
aifix mutate <repo>         人造变异生成冒烟任务集      --max-tasks / --scope / --seed
aifix eval <tasks.jsonl>    在任务集上跑评测           --parallel / --label / --budget-per-task / --budget-total
aifix eval-report <...>     把若干轮结果渲染成对比表
aifix replay <run_id>       回放一次 run 的逐步复盘     --step / --full
aifix ingest                把各次 run 的事实灌进 SQLite
aifix stats                 跨 run 汇总
```

每个子命令的 `--help` 都写了取舍，不只是参数说明。

## 配置

环境变量前缀 `AIFIX_`，嵌套用 `__`（`AIFIX_FIXER__MODEL`）。常用的几个：

| | 默认 | |
|---|---|---|
| `AIFIX_BUDGET_USD` | `2.0` | 需要 `AIFIX_PRICE_MAP`，否则显式设置时**启动即拒绝** |
| `AIFIX_BUDGET_TOKENS` | `500000` | |
| `AIFIX_BUDGET_WALL_SECONDS` | `1800.0` | |
| `AIFIX_MAX_ATTEMPTS` | `3` | 每个 failure 最多试几轮 |
| `AIFIX_MAX_DIFF_LINES` | `300` | 超过即判为整文件重写 |
| `AIFIX_CONSECUTIVE_FAILURE_LIMIT` | `3` | 连着几个没修好就中止整个 run |
| `AIFIX_PRICE_MAP` | `{}` | `{"模型名": [输入价/1k, 输出价/1k]}` |

**为什么没配价格表就拒绝启动**：不配价格表时成本恒为 0，美元闸永远不会触发。你设了上限，系统欣然接受，然后一分钱不拦。与其给一个假的保证，不如现在就停。

完整列表见 `src/aifix/config.py`——每个字段的注释都写了它为什么存在、默认值是怎么定的。

## 项目状态

- **546 个测试**，全绿（2026-07-29 实测。全量耗时 378 / 384 / 466 / 581 / 678 秒——同一台机器连跑五次，最大差 79%，所以这里给的是四个读数而不是一个"权威"数字；本机装了 `mvn`，Maven 那批是真跑的）。约 5,400 行实现、10,000 行测试
- 依赖 [ai-harness-framework](https://github.com/sumengnan/ai-harness-framework)（同作者，提供 AgentLoop / 沙箱 / 预算 / 遥测）
- 第一阶段（M1 闭环 → M2 靠谱 → M3 可度量 → M3b 成本闸 → M4 有结论 → M5 跨语言与可诊断）已完成

**还没做的**（都是有意留的，不是忘了）：覆盖率差分、SWE-bench Lite / Defects4J 的对外可比数字、任务/issue 驱动（输入变成自然语言，系统先写复现测试）、自动开 PR、第三个适配器。

**明确不做**：Web UI、Reviewer agent、主动扫描驱动、自动 merge。

## 这个项目为什么长这样

它是一个 **agent harness 工程**的练习：围绕一个随机的模型，搭确定性的脚手架——循环控制、上下文管理、工具面与权限、状态与断点、验证、观测、预算闸。

开发过程中反复撞上同一种失效模式，撞了**十次以上**：

> **不崩溃、不报错、测试全绿，只有数字或承诺是假的。**

报告里的成本显示 `$0.00`（三个不同的文件各犯一次）；定位准确率量的其实是模型的路径书写风格；「修复 1/1」记的是一个压根没轮到的用例；一个精确措辞但从没验证过的预算上界实际超支 4 倍；`assert "0%" in "100%"` 恒为真；一个测试整体替换了被 patch 的函数，代码提前返回、断言恒真却一直是绿的；「不许改测试文件」的守卫能被一行伪造的 diff 头绕过，而新加的测试只覆盖老实路径，**给了它一个不配得的绿灯**。

所以这个仓库里有些看起来过度的东西：几乎每条断言都配着反向对照，注释里大量写「为什么不这么做」，实测数字带着日期，被证伪的判断在文档里留痕更正而不是悄悄删掉。**在一个专门用来判定别人对不对的系统里，自己说谎的代价格外高。**

---

*仓库尚未添加 LICENSE 文件。在补上之前，请按「保留所有权利」对待。*
