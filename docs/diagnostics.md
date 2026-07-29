# 诊断：出了问题怎么查

这份文档回答一个问题：**一次 run 跑完了，结果不对劲，从哪儿开始翻。**

流程见[架构](architecture.md)。

---

## 产物布局

每次 run 往 `<repo>/.aifix/runs/<run_id>/` 落三份东西：

```
.aifix/runs/2ace03ce/
├── events.jsonl    模型每一步看到什么、决定做什么（回放用的原始素材）
├── facts.jsonl     领域判断的结论（verdict / rollback / flaky…），也是评测直接取用的数据源
└── report.md       渲染给人看的报告
```

run 期间这里还有一个 `tree/`（agent 的 worktree），run 结束时被删掉。开了 `enable_checkpoint` 的话另有一个 `checkpoint.sqlite`。

**事实与事件分开落盘**，这是一条数据契约而不是排版偏好：事实是结论，事件是过程；报告是渲染，事实是数据。跨 run 的轨迹表只能从事实取，不能拿正则去解自己渲染的 markdown —— 报告改一个字，那几列就静默变成 `NULL`，而聚合查询照跑不误、给出的每个数都是「看着正常」的错数。

真实的 `facts.jsonl`（一次 `--dry-run`）：

```json
{"run_id": "2ace03ce", "key": "baseline_failures", "value": 1}
{"run_id": "2ace03ce", "key": "dry_run", "value": true}
{"run_id": "2ace03ce", "key": "adapter", "value": "pytest"}
{"run_id": "2ace03ce", "key": "branch", "value": "aifix/2ace03ce"}
{"run_id": "2ace03ce", "key": "fixed", "value": 0}
{"run_id": "2ace03ce", "key": "spent_tokens", "value": 0}
{"run_id": "2ace03ce", "key": "spent_usd", "value": 0.0}
```

failure 级的事实还带 `failure` 与 `attempt` 两个字段，那是它的**归属**。

---

## 三层嵌套 trace

```
aifix.run
└── aifix.failure          （每个失败用例一段）
    └── aifix.attempt      （每次修复尝试一段）
```

框架自己的 span（`run` / `step` / `model_call` / `tool_call:*`）会自动挂在这三层下面 —— OpenTelemetry 的 span 是天然嵌套的，app 层只要在对的位置开 span。

同一份归属也被写进 `events.jsonl` 的每一行。**这不是冗余**：一次 run 会开好几段 `AgentLoop`（detect 一段、fix 每一轮守卫重试各一段），首尾相接写进同一个文件，落盘之后按位置猜归属只能猜出一条**错位的**时间轴。归属只能由写的这一侧带上。

---

## `aifix replay` —— 逐步复盘

把两份 jsonl 渲染成可读的时间轴。输出是**一次性文本**：可 grep、可重定向、可整段贴给别人。不做交互式 TUI —— 最常见的用法是「跑一遍、翻到出问题那一步、把那几行贴出去」。

```console
$ aifix replay 2ace03ce --repo /tmp/aifixdemo
aifix 回放 · run 2ace03ce
运行目录：/private/tmp/aifixdemo/.aifix/runs/2ace03ce
事件 0 条 · 事实 7 条 · 共 0 步

── 事实 · run 级 ──
  baseline_failures：1
  dry_run：true
  adapter：pytest
  branch：aifix/2ace03ce
  fixed：0
  spent_tokens：0
  spent_usd：0.0
```

两个参数：

- `--step N` 只看第 N 步。用的是**全局步号**：一次 run 会开好几段 `AgentLoop`，每段内部的步号都从 1 重新数，这里按全局顺序重新编过，**与单段会话里的步号对不上**
- `--full` 不截断长文本（补丁、工具返回常有几千字）。默认每个字段截到 2000 字符，**截断处一定留标记**

模型的流式增量（`TextDelta` / `ReasoningDelta`）先拼回整句再渲染 —— 端点一次只吐几个 token，一条一行的话一句话会摊成十几行，而按单条截断时 2000 字符的阈值永远不会触发。

**推理另有一条更短的线：默认只留 200 字符、压成一行。** 正文是结果（诊断 JSON、最终回复），推理是过程，两者该看的量差一个数量级；共用一个阈值时，调到能读推理，补丁就被截没了。判断「模型是真理解了还是在凑断言」时才需要推理全文，那时用 `--full`。

领域事实按其所属的 failure 与 attempt 插进时间轴的对应位置，而不是全堆在末尾 —— 全堆末尾的话，第一个 failure 的 `verdict` 排在第二个 failure 的步骤之后，读的人得在两处之间来回翻。run 级的事实（`baseline_failures`、`dry_run`、`abort`）不属于任何一次尝试，按产物原序排在时间轴前后。

`run_id` 不存在时**列出这个仓库里现有的 run**，并以退出码 1 退出：

```console
$ aifix replay nosuchrun --repo /tmp/aifixdoc
找不到运行目录：/private/tmp/aifixdoc/.aifix/runs/nosuchrun
  aifix 每次 run 会把轨迹写到 <repo>/.aifix/runs/<run_id>/。
  上级目录 /private/tmp/aifixdoc/.aifix/runs 也不存在 —— 确认一下 repo 路径，或者这个 repo 还没跑过 aifix run。
$ echo $?
1
```

诊断工具在数据比自己新的时候应该**退化，不应该崩**：坏行只计数不抛（被 kill 的 run 会在末尾留下半行），未知事件类型原样打印 `data`，缺少 `events.jsonl` 时把还在的事实照常渲染出来。

---

## `aifix ingest` + `aifix stats` —— 跨 run 轨迹

单次 run 的分析 jsonl 撑得住（一个目录、几十行、grep 就够）。但「这个模型最近十轮的定位准确率趋势」「哪一条守卫触发得最频繁」问的是**按 key 聚合、按时间排序、按 run 关联** —— 那是 SQL 干的事。

```console
$ aifix ingest --repo /tmp/aifixdoc
已灌库 1 个 run → /private/tmp/aifixdoc/.aifix/trajectory.db

$ aifix stats --repo /tmp/aifixdoc
aifix 跨 run 统计 · /private/tmp/aifixdoc/.aifix/trajectory.db

── 按适配器 ──
  pytest：run 1 次 · 修复 1 个用例

── 守卫触发（按次数降序）──
  empty_diff：1 次

── 可疑信号最多的 run ──
  a1b2c3d4：1 条
```

### 三条要记住的性质

**幂等。** 同一批产物灌任意多次，表里的行数不变（先删后插：`run_id` 不是 `facts` 的主键，`INSERT OR REPLACE` 对它无能为力；少了那一句，重灌一次所有聚合数字就翻一倍，不报错、不崩溃，只是从此以后每个数都是错的）。所以 `ingest` 报的是**本次处理**的 run 数，不是新增数 —— 重灌同一批仍报同一个数字，这正是「重灌安全」看得见的样子。

**不在 run 结束时自动落库。** 那等于给核心循环加一条可能失败的写路径 —— 磁盘满、db 被别的进程锁住、schema 对不上，任何一个都会把「测试已经修好、补丁已经提交到交付分支」的一次 run 变成一次失败。这张表是**事后诊断用的**，晚几分钟没有代价。

**无事可灌就不建库。** 一个 run 都没找到时 `ingest()` 直接返回，不碰磁盘 —— 「db 文件在不在」是 `aifix stats` 唯一的判据，凭空建出来的空库会把「还没灌过库，先去 ingest」那句提示永久换成三个空小节 + 退出码 0，而那正是下面要说的那件事。

```console
# 全新目录，还没 ingest 过
$ aifix stats --repo /tmp/aifixfresh
还没有灌过库：/private/tmp/aifixfresh/.aifix/trajectory.db 不存在。
  先跑 `aifix ingest --repo /tmp/aifixfresh`，再回来看统计。
$ echo $?
1

$ aifix ingest --repo /tmp/aifixfresh
没有可灌的 run：/private/tmp/aifixfresh/.aifix/runs 下没有带 facts.jsonl 的目录。

# 没有留下空库，提示照旧
$ aifix stats --repo /tmp/aifixfresh
还没有灌过库：/private/tmp/aifixfresh/.aifix/trajectory.db 不存在。
  先跑 `aifix ingest --repo /tmp/aifixfresh`，再回来看统计。
$ echo $?
1
```

读写两侧的克制必须成对：`query_stats` 对不存在的库返回空结果而**不顺手建一个空库出来**，写那一侧在无事可写时也不能把它抵消掉。触发路径很日常 —— `--repo` 打错一次，此后那个错路径上的 `stats` 就永远给三个空小节，再也不提示你去 ingest。这正是 `_cmd_stats` 的注释想避免的读法：「渲染一张空表会被读成『这个仓库没跑过 run』，而事实是没灌过库」。**空表不代表没跑过 run，只代表库里还没有 run 记录。**

「不建库」是「不碰库」，不是「先删再看要不要建」：run 目录是随时可以清理的临时产物，这张表是长期资产，清掉产物再灌一次不会抹掉已有历史（`tests/test_trajectory.py::test_无事可灌时不删已有的库` 钉住这一条）。

---

## 「取不到」与「是 0」必须分开

**这个项目为「假的 `$0.00`」栽过三次**（报告一次、跨模型对比表一次、回放一次）。

没配 `AIFIX_PRICE_MAP` 时框架算出的成本恒为 0。这个 0 与「真的没花钱」区分不了，所以一律当作**不知道**：

| 位置 | 「不知道」长什么样 |
|---|---|
| `report.md` | `- 成本：未知（未配置 AIFIX_PRICE_MAP）（48,213 tokens）` |
| `facts.jsonl` / `trajectory.db` | `spent_usd` 存 **`NULL`**，不是 `0.0` |
| `aifix eval-report` | `未知（未配置 AIFIX_PRICE_MAP）` |
| `aifix replay` | `成本：未知`（**不带原因** —— 这一层只看到某条事件的 `cost_usd` 是 0 或缺失，它不知道价格表配没配。价格表配好了而这一步没发生模型调用时，「未配置 AIFIX_PRICE_MAP」就是一句假话，读的人会照着它去改一个没问题的配置） |

判据是 `tokens > 0 and usd == 0.0`（`nodes/report.cost_is_unknown`）。花了 token 却算出 0 元 —— 那就是没配价格表。

同一条原则贯穿别处：

- **修复数取不到时印破折号，不印 0。** 写 0 等于替库里没有的数据下「一个都没修好」的结论
- **`sum()` 跳过 `NULL` 是最阴的一种。** 「1 次修好 2 个 + 2 次不知道」聚合出来是 2，与「3 次一共修好 2 个」逐字节相同 —— 读的人拿到一个看着正常的假数字，而且**没有任何线索能发现它是假的**。所以 `query_stats` 必须把 `sum(fixed IS NULL)` 与 `sum(fixed)` 一起取，渲染成 `修复 ≥2 个用例（不完整：3 次 run 里有 2 次取不到修复数）`
- **`aifix eval-report` 的 `n = 0` 印 `—` 而不是 `0%`**，见[评测](evaluation.md)的「样本量诚实性」一节

---

## 相关文档

- [架构](architecture.md) —— trace 在哪些位置开 span
- [安全边界](safety.md) —— 守卫触发时记的是哪些 fact
- [评测](evaluation.md) —— `facts.jsonl` 里哪些 key 参与打分
