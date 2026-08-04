# 命令参考

所有命令都可以用 `aifix <命令> --help` 看到内置说明，这份文档补充的是
**每个命令的退出码语义**和**什么时候该用哪一个**。

```
aifix
├── run          修复当前 repo 的失败测试                ← 主命令
├── answer       回答上次 run 提出的问题，带着答案重跑
├── reproduce    把一段缺陷报告译成一条复现测试
├── mine         从 git history 挖任务集
├── mutate       人造变异生成冒烟任务集
├── eval         在任务集上跑评测
├── eval-report  把若干轮结果渲染成对比表
├── replay       回放一次 run 的逐步复盘
├── ingest       把各次 run 的事实灌进 trajectory.db
├── stats        跨 run 汇总
└── issue handle 处理一次 GitHub issue_comment 事件
```

`python -m aifix.cli` 与控制台脚本 `aifix` 指向同一个入口。

---

## `aifix run`

```
aifix run [repo] [--test TEST_ID] [--budget CNY] [--dry-run] [--quiet]
```

主命令。跑一遍完整循环：preflight → baseline → (detect → fix → verify)* → report。

| 参数 | 说明 |
|---|---|
| `repo` | 目标仓库路径，默认当前目录 |
| `--test TEST_ID` | 只修这一个失败用例。队列会被筛成一条 |
| `--budget CNY` | 本次 run 的人民币上限。**需要配置 `AIFIX_PRICE_MAP`**，否则当场拒绝启动 |
| `--dry-run` | 只跑 preflight + baseline，报告有多少活。**不调用任何模型，不花一分钱** |
| `--quiet` / `-q` | 不印进度 |

**进度走 stderr，报告走 stdout。** 所以 `aifix run . > report.md` 存出来的文件顶上
不会粘着几十行进度。

### `--budget` 的准确语义

「越线之后**不再发起新的模型调用**」，不是「绝不超过这个数」。成本只有在调用返回后
才知道，所以越线时那一次调用必然已经花掉了。**超支上界是可陈述的：一次模型调用。**

### 退出码

| 退出码 | 什么情况 |
|---|---|
| 0 | 正常收场，**包括预算耗尽**（活干到钱花完为止，结论仍然可信） |
| 1 | 这次 run **没跑成**：崩溃 / baseline 全是收集错误 / 模型端点不通 / preflight 拦下 |

退 1 时**报告仍然会先印出来** —— 分支上可能真躺着可合并的修复（前面几个 failure
已经交付了，只是最后崩了）。

---

## `aifix answer`

```
aifix answer <编号> [repo] [--run-id RUN_ID] [--budget CNY] [--quiet]
```

上一次 run 里模型调用了 `ask_user` 停下来问了个问题（问题和选项印在报告里），
这个命令把答案带回去。

```bash
aifix run .
# 报告里：
#   ## 需要你回答一个问题
#   **购物车为空时 total() 应该返回什么？**
#   1. 返回 0
#   2. 抛 EmptyCartError

aifix answer 1
```

`--run-id` 不填就用最近那个 —— 问题是刚才印在屏幕上的，不该再要人去翻一个哈希串。

### 它是**重新跑一遍**，不是从断点继续

代价是多跑一次 baseline，换来的是没有需要保鲜的中间状态。

为什么这么选：issue 那条路上根本没有断点可恢复（GitHub Actions 的 job 是一次性的，
容器连同一切中间状态一起消失）。两条入口用同一套语义，才不会各错各的。

重跑时只跑当初卡住的那一个用例：其余的要么上一轮已经修好并进了交付分支，要么与这个
问题无关。

答过的问题会被清掉 —— 留着的话下次 `aifix answer` 会一直翻出这个已经被回答过的问题。

编号**从 1 数起**，越界当场拒绝。放过去的话它会静静地按另一个选项去改代码，而人以为
自己选的是刚才屏幕上那一条。

---

## `aifix reproduce`

```
aifix reproduce [repo] --issue-text FILE [--title TITLE] [--keep]
```

读一段自然语言的缺陷报告，写出一条复现测试并验证它在当前代码上**红着**，然后停下。
**不调用 fixer，不改任何产品代码。**

| 参数 | 说明 |
|---|---|
| `--issue-text FILE` | 缺陷报告的文本文件。**首行当标题、其余当正文**，与 git commit message 同形 |
| `--title TITLE` | 覆盖标题；给了它，`--issue-text` 整个文件都算正文 |
| `--keep` | 保留写下去的测试文件。默认跑完删掉 |

`--issue-text` 与 commit message 同形不是巧合：拿历史上真实的修复 commit message
直接喂进来就能量准确率，`aifix-repro-eval.yml` 那个 workflow 正是这么用的。

### 退出码

**0 表示复现成功且红检通过**，其余情况都是 1。这是个诊断命令，它问的问题就是「能不能
复现」，退出码回答的是那个问题。

> `aifix issue handle` 的口径不同：那边「写不出复现」是一条正常结论，退 0。

### 撞名会改名，不会覆盖

模型给出 `tests/test_calc.py` 而仓库里已经有这个文件时，会改写到
`tests/test_calc_aifix.py` 并同步改写目标用例 id。**绝不覆盖仓库里已有的测试文件** ——
覆盖的后果是整份 `test_calc.py` 被一条生成的测试替换，随后「这个补丁没弄坏别的」在
一个少了一堆用例的对照组上成立。

---

## `aifix mine`

```
aifix mine [repo] [--limit N] [--max-tasks N] [--out PATH]
```

从 git history 里挖评测任务集。

做法：找出让测试从红变绿的 commit `C` → 任务 = checkout 到 `C^` 但保留 `C` 里的测试
文件 → 期望 = 补丁让那条测试转绿且不引入回归 → **`C` 里的源码改动就是标准答案**。

自带 ground truth，分布真实 —— 不需要人来标注，也不会像人造变异那样在分布上跑偏。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit` | 50 | 回溯多少个提交 |
| `--max-tasks` | 10 | 最多产出多少个任务 |
| `--out` | `evals/tasks.jsonl` | 输出路径 |

**一个 target_test 一个任务**（不是一个 commit 一个任务）：一个 commit 修好多个测试
时产出多个任务，这样每个 `TaskResult` 才能保持单一 verdict、单一 attempts 的形状。

挖任务要在克隆出来的工作树里**真跑测试**，所以它和 `run` 一样吃「用哪个解释器」这个
问题。用错的表现是「0 个可用用例」—— 与「这个仓库最近没有红转绿的提交」一模一样。

---

## `aifix mutate`

```
aifix mutate [repo] [--max-tasks N] [--max-new-failures N] [--scope smart|full]
             [--seed N] [--out PATH]
```

在一份**全绿**的母本仓库上人造单点变异（把 `<` 改成 `<=`、`+` 改成 `-`、`True` 改成
`False` 之类），跑测试确认真的弄红了，落成任务。

> **产出的是冒烟集，不是基准。** 变异的分布与真实 bug 不同 —— 它便宜、确定、可任意
> 规模，用途是验证「挖任务 → 跑 agent → 打分」这条链路本身通不通。拿它跨模型比高低
> 是过度解读。所以两类任务的成绩在对比表里是**分开报**的（`origin` 列）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-tasks` | 10 | 最多产出多少个任务 |
| `--max-new-failures` | 5 | 一个变异最多允许弄红几个用例，超过即丢弃 |
| `--scope` | `smart` | `smart` 只跑与被变异文件词干相关的测试文件（快，会漏）；`full` 每个变异跑一次全量 |
| `--seed` | 0 | 变异选点的随机种子，固定后同一个仓库产出可复现 |

`--max-new-failures` 的作用域跟着 `--scope` 走：`smart` 下它只约束**词干匹配到的那几个
测试文件内**的新失败数，全仓可能红得更多。

### 两种中止

- **母本不是全绿**（`UnusableBaseline`）→ 当场停下并指路：先把那些用例修绿，或者换用
  `aifix mine`（它从 git history 里挖真实的红转绿 commit，不要求当前 HEAD 全绿）。
- **产出的任务 id 撞车**（`DuplicateTaskIds`）→ 已验证的那批会被捞到 `<out>.partial`，
  而 `<out>` 一个字节都不写。验证一个候选要真跑一遍测试，一轮变异跑掉半小时是常事 ——
  让异常裸穿的话半小时的成果一个不落盘。`.partial` **不是可用的任务集**，去重之后才能
  拿去 eval。

---

## `aifix eval`

```
aifix eval <tasks.jsonl> [--parallel N] [--label LABEL] [--out PATH]
           [--budget-per-task CNY] [--budget-total CNY]
```

在任务集上跑评测。每个任务：克隆仓库 → 还原到 base commit → 嫁接测试 → 跑一次完整的
`run_once` → 与 ground truth 比对。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--parallel` | 4 | 并发任务数 |
| `--label` | fixer 的 model | 这一轮的模型标签，会进结果文件名 |
| `--out` | `evals/results-<label>.jsonl` | 逐任务明细 |
| `--budget-per-task` | — | 每个任务的人民币上限 |
| `--budget-total` | — | 整批的人民币上限 |

跑完直接印一张对比表（按任务来源分行），明细落成 jsonl。

### `--budget-total` 的超支上界

检查发生在**派发时**，按并发上限预留额度。所以整批超支上界 =
**并发数 × 一次模型调用的成本**，而不是随任务数线性放大。

这个上界是被一次真实的 4 倍超支逼出来的：早先的写法只在任务跑完后才累加已花销，
`--parallel 4` 时四个并发槽位会在派发前全部读到同一个旧数字，各自以为还有整批上限
那么多额度可花。

### `ask_user` 在评测里是关掉的

**评测里没有人能回答。** 留着它等于给模型一条烧钱的岔路：把一整轮花在一个永远等不到
回复的问题上，然后被判成没修好 —— 而那个失分记的是模型的账，实际是评测环境的账。

### 「评测故障」不进成功率的分母

这几类被记成 `error` 而不是失败的 verdict：

- 克隆失败 / baseline 没复现目标用例（任务本身失效）
- baseline 全是收集错误（这台机器缺依赖）
- 模型端点不可达（这台机器出不了网）
- 运行崩溃（aifix 自己的 bug）
- 墙钟预算耗尽（评测调度器的属性）
- 整批预算耗尽被跳过

把它们记进分母，等于让被测模型替我们的环境和 bug 背锅。

---

## `aifix eval-report`

```
aifix eval-report <results.jsonl> [<results.jsonl> ...]
```

把若干轮结果渲染成一张跨模型对比表。同一个结果文件里混了两种任务来源时，会逐份拆开
再拼到一起。

怎么读那张表见 [evaluation.md](evaluation.md)。

---

## `aifix replay`

```
aifix replay <run_id> [--repo PATH] [--step N] [--full]
```

把一次 run 落下的 `events.jsonl` / `facts.jsonl` 渲染成可读的时间轴。

| 参数 | 说明 |
|---|---|
| `--step N` | 只看第 N 步。用的是**全局步号**（从 1 数起）—— 一次 run 会开好几段 AgentLoop，每段内部的步号都从 1 重新数，这里按全局顺序重新编过 |
| `--full` | 不截断长文本（补丁、工具返回常有几千字）。默认每个字段截到 2000 字符，截断处一定留标记 |

`run_id` 不存在时会**列出这个仓库里现有的 run**，并退 1 —— 诊断工具的第一要务是让人
找得到东西，而退 0 的话流水线里「run_id 打错了」和「回放成功」没有任何区别。

---

## `aifix ingest`

```
aifix ingest [--repo PATH] [--runs-dir DIR]
```

扫 `<repo>/.aifix/runs/*/facts.jsonl` 落进 `<repo>/.aifix/trajectory.db`，供
`aifix stats` 查询。

**幂等** —— 同一批产物灌任意多次，表里的行数不变。所以它报的是「本次处理的 run 数」
而不是「新增数」。

**run 结束时不自动灌库**：那等于给核心循环加一条可能失败的写路径（磁盘满、db 被锁、
schema 对不上），而这张表是事后诊断用的，晚几分钟没有代价。

`--runs-dir` 是给 GitHub Actions 用的：runner 是临时的，每次 run 的产物各自消失，
默认目录下永远只有一个 run。把 `aifix/traces` 分支 clone 下来指到这里，历史就重新
连成一片。

**找不到产物时不建库**：空库一旦存在，`aifix stats` 那句「还没灌过库，先去 ingest」
的提示就永远不再出现，取而代之的是三个空小节 + 退出码 0。

---

## `aifix stats`

```
aifix stats [--repo PATH]
```

跨 run 汇总，三张小结：

```
aifix 跨 run 统计 · /path/to/repo/.aifix/trajectory.db

── 按适配器 ──
  pytest：run 12 次 · 修复 ≥18 个用例（不完整：12 次 run 里有 3 次取不到修复数）
  maven：run 2 次 · 修复 3 个用例

── 守卫触发（按次数降序）──
  empty_diff：7 次
  huge_diff：2 次

── 可疑信号最多的 run ──
  a1b2c3d4：3 条
  e5f6a7b8：1 条
```

「修复 ≥18（不完整：…）」这个形状是有意的。SQL 的 `sum()` 跳过 NULL，「1 次修好 2 个
+ 2 次不知道」聚合出来是 2，与「3 次一共修好 2 个」逐字节相同 —— 读的人拿到一个看着
正常的假数字，而且没有任何线索能发现它是假的。

库不存在时**不渲染空表**，而是给一句人话：「还没有灌过库，先跑 `aifix ingest`」。
空表会被读成「这个仓库没跑过 run」，而事实是没灌过库。

---

## `aifix issue handle`

```
aifix issue handle [--repo PATH] [--event PATH]
```

在 GitHub Actions 里被 `issue_comment` 事件调起。整条流水线一次跑完，中途不停下来
等人签字 —— 唯一那道人闸在最终的 PR 上。

`--event` 默认取环境变量 `GITHUB_EVENT_PATH`（Actions 会写好）。本地调试忘了设这个
变量是最常见的第一次失败，所以那种情况会给一句指路的话而不是 `FileNotFoundError`。

### 退出码

**只有崩溃时才非 0。** 写不出复现、没修好都是正常结论 —— 让它们退非 0 的话，Actions
页面会满屏红叉，而其中大半根本不是错误。

具体的非 0 情况：环境类中止（crash / collect / model / preflight）、分支推不上去、
PR 没开成。

详见 [issue-driven.md](issue-driven.md)。

---

## 命令之间的关系

```
                     ┌─→ aifix run ────────→ 分支 + 报告
一个红着的仓库 ──────┤
                     └─→ aifix answer ─────→ 分支 + 报告（重跑）

一段缺陷报告 ────────→ aifix reproduce ───→ 一条红着的测试（只到这儿）

一条 /aifix 评论 ────→ aifix issue handle → 复现 + 修复 + PR

                     ┌─→ aifix mine ───┐
一个有历史的仓库 ────┤                 ├─→ tasks.jsonl ─→ aifix eval ─→ results.jsonl
                     └─→ aifix mutate ─┘                                    │
                                                                            ▼
                                                              aifix eval-report（对比表）

跑完之后：
  .aifix/runs/<id>/ ─→ aifix replay（单次复盘）
                    └→ aifix ingest ─→ trajectory.db ─→ aifix stats（跨 run 汇总）
```
