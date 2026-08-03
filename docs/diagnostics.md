# 诊断：跑完之后怎么查

一次 run 花了钱、跑了几分钟、给出一个结论。这份文档讲的是：**结论不对劲时，去哪儿看它
到底做了什么。**

四样工具，从细到粗：

```
终端进度        跑的时候实时看                  progress.py
aifix replay    单次 run 的逐步复盘             replay.py
aifix stats     跨 run 汇总                     trajectory.py
aifix/traces    让 CI 上的结论活过 runner       traces.py
```

---

## 目录

- [产物长什么样](#产物长什么样)
- [事实与事件：一条重要的区分](#事实与事件一条重要的区分)
- [跑的时候：终端进度](#跑的时候终端进度)
- [跑完之后：aifix replay](#跑完之后aifix-replay)
- [跨 run：ingest 与 stats](#跨-runingest-与-stats)
- [CI 上：aifix/traces 孤儿分支](#ci-上aifixtraces-孤儿分支)
- [两个典型的排查流程](#两个典型的排查流程)
- [fact 键速查](#fact-键速查)
- [OpenTelemetry](#opentelemetry)

---

## 产物长什么样

```
<repo>/.aifix/
├── runs/<run_id>/
│   ├── tree/              worktree —— run 结束时被删掉
│   ├── events.jsonl       模型每一步看到什么、决定做什么、什么时刻（体积大）
│   ├── facts.jsonl        领域判断的结论（数据契约）
│   ├── report.md          给人看的报告
│   ├── pending.json       待人回答的问题（只有停在提问上时才有）
│   └── checkpoint.sqlite  只有 AIFIX_ENABLE_CHECKPOINT=1 时才有
└── trajectory.db          跨 run 汇总表（要先跑 aifix ingest）
```

`.aifix/` 在 `.gitignore` 里 —— 它是运行产物，不该进入任何一次 diff。

---

## 事实与事件：一条重要的区分

| | `events.jsonl` | `facts.jsonl` |
|---|---|---|
| 是什么 | 模型每一步看到什么、决定做什么、什么时刻 | 领域判断的结论 |
| 谁消费 | `aifix replay`，出问题时人来读 | **评测直接取用**、`aifix stats` 灌库 |
| 体积 | 大（模型 IO 原文），三份里唯一会失控的 | 小（几十行） |
| 保留 | CI artifact（90 天） | 永久（推到 `aifix/traces` 分支） |

### 为什么这条区分很要紧

**事实是数据契约，报告是渲染，两者不该反过来。**

「适配器、分支、修复数、花销」这四类值以前只存在于渲染出来的 `report.md` 里，跨 run 的
轨迹表只能拿正则去解自己渲染的 markdown —— **报告改一个字，那几列就静默变成 NULL，而聚合
查询照跑不误、给出的每个数都是「看着正常」的错数。**

现在它们各自落一条 fact，正则那份只留给已经落盘、再也不会重新生成的老目录。

同一条理由还体现在别处：baseline 不只记「有 35 个红」，还记
`baseline_failure_ids`（那 35 个分别是谁）—— 实测有一次里面 35 个红全是 aifix 自己造成的
（环境泄漏），而查清它们是谁只能靠 PR 正文里那段给人看的告警。**诊断数据要能被程序查，
否则统计永远看不出「哪些红是环境造成的」。**

### 事件也要带归属

一次 run 会开好几段 `AgentLoop`（detect 一段、每次守卫重试各一段），首尾相接写进同一个
文件。`event_to_dict` 只给 type 与 data，落盘之后就再也认不出「这一步是修哪个用例的第几次
尝试」。

**按位置猜归属只能猜出一条错位的时间轴** —— 所以归属由写的那一侧带上（`failure` /
`attempt` 两个字段并进每一条事件）。

---

## 跑的时候：终端进度

`run_once` 一次要跑几分钟起步。以前这几分钟里终端一个字都没有 ——「在干活」和「卡死了」
长得一模一样。

现在会实时印：

```
run a1b2c3d4 · pytest · aifix/a1b2c3d4
baseline：跑了 214 个，红 3 个（1:07）
[1/3] tests/test_cart.py::test_total
  第 1 轮 / 共 3 轮
  诊断：src/shopcart/cart.py（1,842 tokens）
   1 ▸ read_symbol  Cart.total
   1 ✓ src/shopcart/cart.py 第 40-58 行
   2 ▸ edit_file  src/shopcart/cart.py
   2 ✓ 已修改（第 47 行）
   3 ▸ run_tests  test_total
   3 ✗ 1 failed
  ...
  补丁：src/shopcart/cart.py（+3 −1）
  判定：已修复（52s）
...
完成：修复 2 / 3 · $0.14（238,070 tokens）
```

（具体渲染以实际输出为准 —— 上面是示意。）

几处设计：

- **进度走 stderr**，报告走 stdout。`aifix run . > report.md` 存出来的文件是干净的。
- **每次工具调用报两条**：开始时报「在干什么」，结束时报「成了还是砸了、为什么砸」。
  只在结束时出声的话，最需要心跳的那几分钟仍然是空屏（`run_tests` 一跑就是几十秒）;
  而不听结束事件的话，失败的调用和成功的长得一模一样 —— 实跑过的一次 run：23 次调用里
  5 次是错的。
- **方法名是语义，不是排版**。节点报告「发生了什么」，怎么渲染由实现决定 —— 让节点去拼
  字符串的话，改一句措辞就要动核心循环。
- **默认实现什么都不做**（`NullProgress`），而且它是 `run_once` 的默认值：`aifix eval` 会
  并行跑几十个任务，默认出声的话几十条进度会交织成一团。
- `--quiet` 关掉。进度本来就走 stderr，只在你连 stderr 一起收进日志时才需要它。

### issue 那条路也有（2026-08-03 补上的）

`aifix issue handle` 在 Actions 上跑几十分钟，而它此前**一个字都不输出** —— 那个默认值
是把双刃剑：谁不传 progress 谁就静默，而 issue 这条路一直没传。实测（issue #9）那一步的
日志只有两行：

```
03:58:34  env 声明
04:27:01  通路：delivered · PR：…
```

中间 28 分半，零行。**卡住的时候，日志是唯一能实时看到的东西** —— artifact 要 job 结束
才下载得到，而那时已经不叫「卡住」了。

现在核心循环那一段接了 `TerminalProgress`，它之外的几段（复现、红检、推分支、开 PR）
也各报一句。非 TTY 下 `TerminalProgress` 自动退成逐行输出，不会往 Actions 日志里灌
`\r` 残句。

---

## 跑完之后：`aifix replay`

```bash
aifix replay a1b2c3d4 --repo /path/to/repo
aifix replay a1b2c3d4 --step 7 --full
```

把 `events.jsonl` / `facts.jsonl` 渲染成可读的时间轴：每一步模型说了什么、调了什么工具、
拿回什么、**花了多久**，以及领域事实插在它所属的那次尝试之后。

```
── 步骤 1 · tests/t.py::x · 第 1 次尝试  4.0s ──
  [ToolStarted] 开始执行工具 read_symbol（id=c1）
  [ToolFinished] 工具返回（成功，id=c1）：…

── 步骤 2 · tests/t.py::x · 第 1 次尝试  1分33秒 ──     ← 一眼看出慢在哪
  [ToolStarted] 开始执行工具 run_tests（id=c2）
  [ToolFinished] 工具返回（成功，id=c2）：1 failed
```

每步耗时来自事件的 `ts` 字段 —— 它是**事件到达那一刻**记的（在 `consume` 里），不是落盘
时补的。落盘是整段 AgentLoop 跑完之后批量做的，在那里打戳会让所有事件挤在同一毫秒上，
算出来的耗时全是 0。

> **老产物没有 `ts`。** 这个字段是 2026-08-03 加的，之前落下的 run 回放时不显示耗时 ——
> 而不是显示 `0s`。没有真实时刻就什么都不说，编一个出来会被读成「这一步是瞬间完成的」。

输出是**一次性文本**：可 grep、可重定向、可整段贴给别人。不做交互式 TUI —— 最常见的用法
是「跑一遍、翻到出问题那一步、把那几行贴出去」，一次性输出正好满足它，交互式反而挡在中间。

### 两个参数

- `--step N` 用的是**全局步号**（从 1 数起）。一次 run 会开好几段 AgentLoop，每段内部的
  步号都从 1 重新数，这里按全局顺序重新编过 —— **与单段会话里的步号对不上**。
- `--full` 不截断。默认每个字段截到 2000 字符，**截断处一定留标记**。

### 几处「诊断工具不该成为第二个故障点」的处理

| 情况 | 做什么 |
|---|---|
| `run_id` 打错了 | **列出这个仓库里现有的 run**，并退 1。诊断工具的第一要务是让人找得到东西 |
| jsonl 有半截坏行（被 kill 的 run 会留下） | 只计数不抛异常，在头部说明「有 N 行解析不了，已跳过」。半截文件也得看得到前半截 |
| 目录在、`events.jsonl` 不在 | 说清楚缺的是什么，然后**把还在的事实照常渲染出来** —— 用户要找的很可能正是那几条（比如中止原因） |
| 事件里有没见过的类型 | 原样打印 data，不崩。**诊断工具在数据比自己新的时候应该退化，不应该崩** |
| 没配价格表 | 印「未知」而不是 `$0.00`。而且**不带原因** —— 这一层看到的只是某条事件里 cost 是 0，它不知道价格表配没配。说错原因比不说原因更糟 |
| 老产物（事件不带 failure/attempt 标记） | 说明这批数据没有可靠的逐步对应关系，事实按自身归属分组列在时间轴之后。**硬按顺序猜能拼出一条看着精确、实则编出来的时间轴** |

---

## 跨 run：`ingest` 与 `stats`

单次 run 的 jsonl 撑得住 —— 一个目录、几十行、grep 就够。但「这个模型最近十轮的定位准确率
趋势」「哪一条守卫触发得最频繁」问的是**按 key 聚合、按时间排序、按 run 关联**，那是 SQL
干的事；用 grep 拼出来的数字既不可信也不可复用。

```bash
aifix ingest --repo /path/to/repo
aifix stats  --repo /path/to/repo
```

### 为什么不在 run 结束时自动灌库

那等于给核心循环加一条**可能失败的写路径** —— 磁盘满、db 被别的进程锁住、schema 对不上，
任何一个都会把「测试已经修好、补丁已经提交到交付分支」的一次 run 变成一次失败。

而这张表是**诊断用的**，事后灌一次就够，晚几分钟没有任何代价。灌库因此是一个独立的、可以
重来的动作 —— 这也是它必须**幂等**的原因（同一批产物灌任意多次，表里的行数不变）。

> 幂等靠的是「先删后插」：`run_id` 不是 facts 表的主键（一个 run 有几十条），
> `INSERT OR REPLACE` 对它无能为力。少了那一句，重灌一次所有聚合数字就**翻一倍** ——
> 不报错、不崩溃，只是从此以后每个数都是错的。

### 表结构与三条数据契约

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, adapter TEXT, branch TEXT,
    baseline_failures INTEGER, fixed INTEGER,
    spent_tokens INTEGER, spent_usd REAL, abort TEXT, abort_kind TEXT
);
CREATE TABLE facts (run_id TEXT, failure TEXT, attempt INTEGER, key TEXT, value TEXT);
```

1. **目录名是权威**。一个 run 目录 = 一个 run_id；facts 行里的 `run_id` 字段只是副本 ——
   目录里混进别的 run_id 时，按副本删会漏删，重灌就翻倍。
2. **`facts.value` 一律是 JSON 文本**。真实产物里 value 有字符串、数字、布尔，也有列表
   （三类信号的 value 就是列表）。一列里混着裸值和 JSON 之后没人能安全地解它。
   代价是查询时必须按 JSON 解 —— `WHERE value='better'` 永远匹配不到，库里是 `"better"`
   （带引号）。
3. **取不到的字段一律存 NULL，不填 0、不填空串。** 花了 token 却记 $0.00 这种假数字，比缺
   一列难查得多。

### `aifix stats` 印什么

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
```

### 「修复 ≥18（不完整：…）」这个形状是有意的

修复数有三种形状，**不能合并成一个数字**：

- 全都解得出 → 合计就是合计
- 一个都解不出 → 破折号。写 0 等于替库里没有的数据下「一个都没修好」的结论
- **混着 —— 最阴的一种**：SQL 的 `sum()` 跳过 NULL，「1 次修好 2 个 + 2 次不知道」聚合出来
  是 2，与「3 次一共修好 2 个」**逐字节相同**。读的人拿到一个看着正常的假数字，而且没有
  任何线索能发现它是假的

所以查询里 `sum(fixed)` 和 `sum(fixed IS NULL)` 必须一起取 —— 后者是前者的**完整性凭据**。

### 库不存在时不渲染空表

```
还没有灌过库：/path/.aifix/trajectory.db 不存在。
  先跑 `aifix ingest --repo .`，再回来看统计。
```

空表会被读成「这个仓库没跑过 run」，而事实是没灌过库。

同一条克制在 `ingest` 那边：**找不到产物时不建库**。`_connect` 空跑一次也足以把文件造出来，
而 `stats` 只认「db 文件在不在」—— 库一旦存在，那句提示就永远不再出现，取而代之的是三个空
小节 + 退出码 0。`--repo` 打错一次就够在那个错路径上永久制造这个假象。

而且是「不碰」而不是「先删再看要不要建」：run 目录是可以随时清理的临时产物，这张表是长期
资产，**清掉产物重灌不该抹掉历史**。

---

## CI 上：`aifix/traces` 孤儿分支

GitHub Actions 的 runner 是临时的：job 一结束，`.aifix/runs/` 连同整台机器一起消失。而
`ingest` / `stats` 扫的正是那个目录 —— **在 Actions 上它下面永远只有本次这一个 run，跨 run
统计天然失效。**

所以 issue 流水线跑完会把这次的**结论**推到一条孤儿分支上：

```
aifix/traces（孤儿分支，永不合并）
└── runs/
    ├── a1b2c3d4/{facts.jsonl, report.md}
    ├── e5f6a7b8/{facts.jsonl, report.md}
    └── ...
```

**只推 `facts.jsonl` 与 `report.md`，不推 `events.jsonl`。** 这正是上面那条区分：事实是
结论，要长期统计所以要永久留；事件只在出问题时才要，扔进 CI artifact（90 天）就够，而且它
是三份里唯一体积会失控的。

**用孤儿分支而不是在 main 上加目录**：trace 是运行产物，不该进入任何一次 diff、不该出现在
任何一次 review 里，也不该让 `git log -- src/` 被它稀释。

把历史重新连成一片：

```bash
git clone --branch aifix/traces <repo> /tmp/traces
aifix ingest --repo . --runs-dir /tmp/traces/runs
aifix stats
```

几处细节：

- **幂等** —— 同一个 run 推两次，第二次无可提交、照样返回成功。Actions 重跑同一个 job 是
  常事，让它把整个 job 弄红是错的。
- **失败不影响交付** —— 补丁已经推上去、PR 已经开了，为了一次归档失败把整个 job 弄红，等于
  让人以为修复没成功。出声但不改结果。
- 建孤儿分支时 `checkout --orphan` **保留当前工作区的内容与索引**，所以紧接着必须清空 ——
  不清的话第一个提交会把整份源码树复制过来，这条永不合并的分支会随 run 数线性长胖，而它的
  用途只是存几十行 jsonl。

---

## fact 键速查

按写入位置分组。这是**数据契约** —— 评测和 `stats` 都直接读它们。

### run 级（不带 failure / attempt）

| key | 什么时候写 |
|---|---|
| `adapter` / `branch` | 每次 run 收尾 |
| `baseline_failures` / `baseline_failure_ids` | baseline 之后 |
| `fixed` / `spent_tokens` / `spent_usd` | 收尾。`spent_usd` 为 `null` 是**有意的事实**：「花了钱，但这次不知道花了多少」 |
| `abort` / `abort_kind` | 中止时 |
| `crash` | 运行异常 |
| `dry_run` | `--dry-run` |
| `test_python` | 用的哪个解释器 |
| `imports_outside_worktree` | 探测到目标包从 worktree 之外导入 |
| `collection_errors_allowed` | 逃生口被打开时 |
| `reproduce_kind` / `reproduce_tokens` | 复现那一步的收场与花销 |

### failure / attempt 级

| key | 含义 |
|---|---|
| `suspect_file` | 诊断点名的文件。**定位准确率只取 `attempt == 1` 的那一条** |
| `suspect_anchor` | 锚点种类：`traceback`（强）/ `import`（弱） |
| `suspect_unanchored` | 一个源码候选都没有，这次是无锚猜测 |
| `suspect_in_traceback` | 模型点名的文件落在 traceback 候选里吗（**不是**定位准确率，那个对 ground truth 判） |
| `diagnosis_parse_failed` | 诊断 JSON 解析不出来 |
| `diff_lines` / `touched` | 这一轮改了多少行、动了哪些文件 |
| `guard_hit` / `guard_giveup` | 守卫触发 / 因同一条守卫连撞而放弃 |
| `ignored_paths` | 改动落在被 .gitignore 盖住的路径上 |
| `violation` | 越界尝试（`test_edit` / `path_escape` / `loop_abort`） |
| `ask_user` | 模型停下来提的那个问题 |
| `verdict` | 三态判定 |
| `rollback` | 判定不是 BETTER，改动被回滚 |
| `flaky_filtered` / `confirmed_regressions` | 抖动过滤的两侧 |
| `baseline_flaky` | 一个字节没改却判 BETTER —— 目标用例本来就是抖的 |
| `patch_cancelled_out` | 动过文件但暂存区为空（补丁被自己的反向补丁抵消了） |
| `delivery_failed` | `git add` / `git commit` 没成功 |
| `removed_public_symbol` / `new_module_state` / `files_outside_suspect` | 三类静态信号，**只有判 BETTER 才写** |
| `signals_discarded` | 被回滚的尝试留下的信号。**刻意不进评测计数** —— 只有诊断价值 |

### 两条容易踩的口径

**信号的单位是「类」不是「个」。** 三类**各写一条** fact，value 是那一类的整个列表。所以
每个交付的补丁至多贡献 3 条。按符号个数展开的话，「在一个文件里删 10 个符号」记 10、
「摊到 20 个文件一个符号没删」记 1，跨模型比这一列就不是同一把尺。

**`signals_discarded` 与那三条不是同一套。** 它是判 SAME / WORSE 后被回滚的尝试留下的。
算进计数的话，「第 1 轮删了公开符号被回滚、第 2 轮干净地修好」会被记成
`fix_hits=1 且 signals≥1` —— 而那正好是「规格套利」的指纹定义，于是指纹变成假的，方向还
偏向爱试错的模型。

---

## OpenTelemetry

`RunTrace` 同时开三层嵌套 span：

```
aifix.run
└── aifix.failure（test_id）
    └── aifix.attempt（第几轮）
        └── 框架自己的 run / step / model_call / tool_call:* span
```

OpenTelemetry 的 span 是天然嵌套的，所以框架那几层会自动挂在这三层下面 —— app 层只要在对的
位置开 span，不需要打通任何东西。

每一条 fact 也会同时打到当前 span 的属性上（`aifix.<key>`）。**没配 provider 时是 no-op，
零开销** —— 所以这段代码永远开着，不需要一个开关。

导出到真正的 OTLP 后端要在框架侧配（`HARNESS_OTEL_ENABLED` 等，见
`ai-harness-framework` 的文档）。

---

## 两个典型的排查流程

### 「它是不是卡住了」（跑到一半，实时）

看 Actions 日志。核心循环的每一步、以及复现/红检/推分支/开 PR 各段都会实时出声：

```
── 读 issue #9，让模型写一条复现测试……
── 复现测试已写下：tests/test_x.py —— 跑一遍确认它真的红了
── 开始修复：tests/test_x.py::test_y
run 1c586c4 · pytest · aifix/1c586c4
baseline：跑了 956 个，红 1 个（3:58）
[1/1] tests/test_x.py::test_y
  第 1 轮 / 共 3 轮
   1 ▸ read_symbol  …
```

**跑完之后**才能拿到 artifact，所以卡住的当下只有日志。日志停在哪一句，就是卡在哪一步。

### 「报告说修好了 2 个，但我看分支上不对劲」（跑完，事后）

```bash
# 1. 先看结论
cat .aifix/runs/a1b2c3d4/report.md

# 2. 看这次的领域事实 —— verdict、rollback、guard_hit、signals 都在这儿
cat .aifix/runs/a1b2c3d4/facts.jsonl | python -m json.tool --json-lines

# 3. 找到可疑的那一步，看模型当时到底做了什么
aifix replay a1b2c3d4 | less
aifix replay a1b2c3d4 --step 12 --full

# 4. 用 git 直接验，别信报告里的数字
git log --oneline main..aifix/a1b2c3d4
git diff main aifix/a1b2c3d4
git diff main aifix/a1b2c3d4 -- tests/     # 必须是空的

# 5. 如果怀疑是系统性问题，看跨 run 的形态
aifix ingest && aifix stats
```

第 4 步值得强调：**「报告说修好了时，分支上必须真的有东西」这条主张要用 git 验，不是读报告
里的数字。** CI 的验收 job 就是这么写的。
