# M4「有结论」设计规格

**日期**：2026-07-28
**前置**：M1（闭环）· M2（靠谱）· M3（可度量）· M3b（成本闸）全部完成并合入 main
**范围来源**：M3 计划末尾「交给 M3b 的缺口」表中的 A 组（静态部分）、C 组（适配层缺陷）、D 组（评测规模化），以及 M3b 进度账本登记的一条零散项

---

## 1. 问题陈述

M3 产出了跨模型对比表，但那张表**只有 1 个任务**。它演示了形状，证明不了任何事。

把它扩到有结论的规模，卡在三处：

**其一，挖掘太贵。** `verify_commit` 对每个候选 commit 跑**两次全量测试**。实测本仓库全量套件 171 秒，一个候选 commit 约 6 分钟；本仓库近 98 个提交里有 65 个是候选（同时改了测试与源码）。挖 20 个任务需要小时级的机器时间。

**其二，适配层有两处缺陷会让挖掘产出错误或产出不了。** `make_test_id` 对类内测试合成出不存在的路径；`split_paths` 只认 `.py`，测试依赖的夹具文件不会被嫁接。

**其三，样本量少时百分比会被误读成结论。** `1/1 = 100%` 与 `12/20 = 60%` 现在渲染成同一种东西。一列 `100%` 读起来像"完美"，实际是"只跑了一个任务"。

外加 M3 真实验收暴露的**规格套利**：模型把 `add` 改成有状态函数去满足一个自相矛盾的断言、顺手删掉无测试覆盖的 `mul`，每一道守卫都正常工作，系统报告「修复 1/1」并给出 merge 命令。

---

## 2. 目标与非目标

**目标**

1. 挖掘从「小时级」降到「分钟级」，且不牺牲任务集的可信度
2. 修掉挡在挖掘路上的两处适配层缺陷
3. 对比表在样本量不足时**自己说出来**，而不是让读者去猜
4. 给规格套利装上零模型调用的静态信号，让人在 merge 之前有东西可看
5. 提供 C 类冒烟集（人造变异），用于不依赖模型的回归验证

**非目标**

- **不引入 Reviewer agent**。规格 §1 定的「审查者是人」不变。本里程碑做的是给人**更好的输入**，不是替人做判断
- **不自动拒绝可疑补丁**。静态信号只标注，不改判定。三态判定仍然只由测试结果决定
- 不做覆盖率差分（A 组里代价最高的一档，留给后续）
- **不代替用户跑那一轮花钱的完整评测**。本里程碑交付的是「能跑出结论的能力」，真跑一轮是外向动作，由用户决定

---

## 3. C 组：适配层缺陷

### 3.1 `make_test_id` 对类内测试产出无效 id

**现状**（`PytestAdapter.make_test_id`）：

```python
path = file or (classname.replace(".", "/") + ".py")
return f"{path}::{name}"
```

pytest 默认 `junit_family=xunit2`，`<testcase>` **不写 `file` 属性**（已实测，pytest 9.1.1）。于是走回退路径：

| classname | name | 现在产出 | pytest 真正认的 |
|---|---|---|---|
| `tests.test_foo` | `test_top` | `tests/test_foo.py::test_top` ✓ | 同左 |
| `tests.test_foo.TestBar` | `test_baz` | `tests/test_foo/TestBar.py::test_baz` ✗ | `tests/test_foo.py::TestBar::test_baz` |

后果不止于挖掘：M2 的 flaky 过滤（`run_scoped` 复跑）与 `run_tests` 工具同样依赖这个 id 能对上真实路径。一个无效 id 进了 pytest 命令行，pytest 在**收集阶段整轮中止**（exit code 4），一个用例都不跑，写出一份 `tests="0"` 的空报告——报告存在，`require_report` 检查不出异常，看起来像"全部复跑通过"。

**修法，两层**：

*层一，消除根因*：`PytestAdapter._BASE` 增加 `-o junit_family=xunit1`。已实测 xunit1 写出 `file='tests/test_foo.py'` 与 `line`，且与 xunit2 在本项目用到的其余结构（`<skipped>` / `<failure>` / `<error>` / `message`）完全一致；pytest 9.1.1 下无 deprecation 警告。`-o` 覆盖目标项目 ini 里的设置。

*层二，回退路径正确化*：`file` 缺失时不再整段替换，而是从 classname 尾部剥掉**首字母大写**的段作为类名：

```
tests.test_foo.TestBar  →  路径 tests/test_foo.py，类 [TestBar]
tests.test_foo          →  路径 tests/test_foo.py，类 []
```

为什么两层都要：层一依赖 pytest 继续支持 xunit1，而这是一个被标记为旧格式的选项；层二是它消失后（以及别的适配器）的兜底，本身也正是缺口表点名要修的东西。

**为什么按"首字母大写"判**：pytest 默认 `python_classes = Test*`，类名必然大写开头；模块名按 PEP 8 是小写。这是启发式，但只在 `file` 缺失时才用得上，而层一保证了正常情况下用不上。

### 3.2 收集错误产出的伪 id 不可重跑

实测（xunit1）：测试文件导入了一个还不存在的名字时，junit 报告写出

```xml
<testcase classname="" name="tests.test_new" file="tests/test_new.py">
  <error message="collection failure"/>
</testcase>
```

`classname` 为空，`name` 是点分模块名。当前 `make_test_id` 产出 `tests/test_new.py::tests.test_new` —— pytest 认不了。

这个形状**是挖掘会大量撞上的**，有实测支撑：本仓库 200 个提交里 65 个是挖掘候选（同时改测试与源码），其中 **32 个新增了测试文件**。抽查 `9c2415c3` / `67ac3269` / `eadc4ba3`，三个都是同一个提交里既新建 `src/aifix/eval/<x>.py` 又新建 `tests/test_eval_<x>.py`——把测试嫁接到父提交上，导入的模块根本不存在，必然是收集错误。「新增测试文件 + 新增被它导入的模块」是 TDD 提交的标准形状，这条路走不通就等于挖掘丢掉近一半候选。

**修法**：`classname` 为空时，id 取 `file`（缺失则退回 `name`），不拼 `::`。`tests/test_new.py` 本身就是 pytest 认的 node id，重跑没有问题。

**连带影响**：这让「整个测试文件在 C^ 收集失败、在 C 正常」成为一个**可用的任务形状**——`target_test` 是文件级 id，baseline 复现它、verify 判它消失，全链路成立。见 §4.3。

### 3.3 `split_paths` 只处理 `.py`

**现状**：非 `.py` 路径直接 `continue`，既不进 `test_files` 也不进 `gold_files`。

**后果**：某个 commit 同时新增了测试所需的非 `.py` 夹具（数据文件、配置片段、快照），该文件不会被 `materialize` 嫁接到任务工作区。任务在 base 侧因缺文件而红、在 C 侧绿，通过全部现有校验进入任务集，但 ground truth 实际不可达——修复模型即便诊断和补丁都对，也可能因为缺夹具而通不过。这不是"捏造任务"（确实是红转绿），是任务质量问题。

**修法**：**测试目录下的**非 `.py` 文件归入 `test_files`，跟着测试一起嫁接。判据是**按路径分段的前缀匹配**（`pp.parts[:len(d)] == d`，`d` 是 `test_dirs` 里那一项切成的分段），不是 `pp.parts[0] in test_dirs`：M5 的 MavenAdapter 走 Maven 标准布局 `src/test/java/...`，`test_dirs` 会是 `["src/test"]`，只看第一段拿到的是 `src`，判不出来，整棵 Java 测试树会被当成源文件塞进 `gold_files`。按分段比而不是裸 `startswith`，是因为 `testdata/x.py` 不是 `test` 目录下的文件。对 pytest 的 `["tests", "test"]` 行为完全不变。

非测试目录下的非 `.py` 文件继续忽略——**不进 `gold_files`**。`gold_files` 是 `locate_hit` 的判定依据，衡量的是 Detector 定位**源文件**的能力；把数据文件塞进去会稀释这个指标。

**顺带修一处同源缺陷**：`conftest.py` 若位于仓库根目录（不在 `test_dirs` 里、也不以 `test_` 开头），当前会被判成源文件进 `gold_files`。它是测试基础设施，不是 ground truth。修法：文件名为 `conftest.py` 的一律归 `test_files`。

---

## 4. D 组：评测规模化

### 4.1 挖掘从两次全量降到一次

**现状**：`verify_commit` 对每个候选 commit 跑两次全量测试（C^ 处一次、C 处一次）。

**观察**：一个 commit 把某个测试从红修到绿，那个测试**几乎必然在这个 commit 改动的测试文件里**——`is_candidate` 已经要求了 commit 同时改测试与源码。全量测试里的其余部分对判定"哪些用例红转绿"没有贡献。

**新流程**：

| 阶段 | 范围 | 状态 | 作用 |
|---|---|---|---|
| 1 | scoped 到 `test_files` | C^ 源码 + C 测试 | 得到 `red` |
| 2 | scoped 到 `test_files` | C | 得到 `green` |
| 3 | scoped 到 `cand` | C | 复跑确认（现有逻辑，不变） |
| 4 | **全量** | 回到阶段 1 的状态 | 确认候选在全量下也红 |

`cand = (red.ids - green.ids) & green.ran`，阶段 3 后 `cand = (cand & recheck.ran) - recheck.ids`，阶段 4 后 `cand &= red_full.ids`。

**成本**：没有候选的 commit 只花两次 scoped（本仓库量级：秒级到十几秒）；有候选的多花一次全量。相比现在的「每个候选 commit 两次全量」，省掉的正是绝大多数——因为多数候选 commit 最终产出 0 个可用用例。

**实测**（用一段一次性探针脚本，在本仓库最近 40 个提交里取前 8 个候选跑阶段 1+2）：

```
51110775 tests=[test_cost_gate_e2e.py]   red=0 green=0 cand=0
fa69ab16 tests=[test_cost_gate_e2e.py]   red=1 green=0 cand=1
79baffa0 tests=[test_nodes_fix_guards.py] red=2 green=0 cand=2
8b88dfe6 tests=[test_cost_gate_e2e.py]   red=1 green=0 cand=1
8839a1b0 tests=[test_cli_args.py]        red=4 green=0 cand=4
6118865b tests=[test_eval_runner.py]     red=1 green=0 cand=1
93d3a8d3 tests=[test_eval_runner.py]     red=3 green=0 cand=3
e3a1cfbe tests=[test_cli_args.py]        red=3 green=0 cand=3
```

8 个候选 commit 产出 15 个可用用例，全程约 13 分钟——其中大头是 `test_cost_gate_e2e.py` 这类会起子进程的 e2e 文件。所有 15 个候选都落在 `green.ran` 里，说明 §4.3 的收集错误在这一批里没有出现（这些提交都是往**已存在**的测试文件里加用例）。

> **事后更正（实现之后的真实测量）**：上面这段探针数据**只跑了阶段 1 和 2**，不含阶段 4 的那次全量确认。据此推算的提速被高估了。
>
> 实现完成后真跑 `aifix mine . --limit 25 --max-tasks 12`：扫 25 个提交、验证 6 个候选、产出 12 个任务，**18 分钟**（约 3 分钟/候选 = 一次全量 + 三次 scoped）。同样这 6 个候选走旧的两次全量路径约需 34 分钟。
>
> **实测提速约 1.9 倍，不是探针数据暗示的 3.7 倍。** 差别全在阶段 4——它把成本从「两次全量」降到「一次全量」，省的是一次，不是两次。真正被省掉的是**没有候选的那些 commit**：它们现在只花两次 scoped，而旧流程要为每一个都付两次全量。所以提速倍数取决于目标仓库的候选命中率，本仓库命中率高（6/6 都有产出），是提速倍数**最低**的那一端。

**为什么阶段 4 不能省**：评测时 `run_task` 用**全量** baseline 复现 `target_test`（`baseline_node` 跑全量）。scoped 下红、全量下绿的用例（顺序依赖、状态污染）会在评测时变成 `error`——安全但浪费。阶段 4 把这个浪费挪到挖掘时，一次性付清。

**为什么阶段 3 仍然保留**：它比阶段 1/2 更窄（只跑候选用例本身），排的是"在测试文件这个范围内碰巧绿了"的情形，与阶段 4 排的（"在全量范围内碰巧红了"）是两个方向。

**回到阶段 1 状态的实现**：`materialize` 完成后立即记下 `git rev-parse HEAD`（它可能是 `base_commit`，也可能是 materialize 为嫁接测试而新建的提交），阶段 4 用 `git checkout --force <该 sha>` 回去。

**已知取舍（写进代码注释）**：commit C 修好了一个它**没有改动**的测试文件里的用例——这种任务会被漏掉。这是召回损失，不是正确性损失；换来的是数量级的提速。

### 4.2 样本量诚实性

**现状**：`render_table` 把 `locate_rate` / `fix_rate` 渲染成 `{:.0%}`。`1/1` 与 `12/20` 长得一模一样。

**修法**：两列都改成 `比例 (命中/总数, 95%CI 下界–上界)`，区间用 **Wilson score interval**：

```
中心 = (p̂ + z²/2n) / (1 + z²/n)
半宽 = z·√(p̂(1-p̂)/n + z²/4n²) / (1 + z²/n)
```

选 Wilson 而不是正态近似（Wald）：`p̂ = 1.0, n = 1` 时 Wald 给出 `[100%, 100%]`——一个宣称"确定无疑"的区间，正是本里程碑要消灭的东西。Wilson 在同样输入下给出 `[21%, 100%]`，一眼看出无结论。

`n = 0` 时不渲染区间，显示 `—`。

**表格宽度**：加区间后每格显著变宽。接受——这张表是拿来读的，不是拿来省地方的。

### 4.3 收集错误作为任务形状

§3.2 的修法让文件级 id 可重跑之后，「测试文件在 C^ 收集失败、在 C 正常」这一形状自动成为可用任务：

- `red` 含文件级 id（如 `tests/test_new.py`）
- `green` 不含它（文件正常收集，发出的是各个用例）
- `cand = (red.ids - green.ids) & green.ran` —— **文件级 id 不在 `green.ran` 里**，于是被排除

这是当前实现的一个**沉默的召回黑洞**：TDD 提交的标准形状被整体挡在门外。

**修法**：`green.ran` 的判据对文件级 id 放宽——若某个 red id 是文件级的（不含 `::`），且在 green 侧该文件对应的用例**至少跑到了一个、且全部通过**，则认定它红转绿。实现上需要 `FailureSet` 能回答"某个文件下 green 侧跑了哪些用例"，而 `ran` 里的 id 形如 `tests/test_new.py::test_x`，按 `::` 前缀即可分组，不需要新增数据结构。

### 4.4 C 类冒烟集：人造变异生成器

**定位（要写进 `--help` 和文档）**：这是**冒烟集**，不是基准。变异的分布与真实 bug 不同——它便宜、确定、可任意规模，用来验证链路本身是否工作；用它跨模型比高低是过度解读。

**命令**：`aifix mutate <repo> --out evals/tasks-mutants.jsonl --max-tasks N`

**前提**：仓库 HEAD 全绿。开跑先做一次全量确认，不绿即拒绝——在一个本来就红的仓库上做变异，分不清红是变异造成的还是本来就有的。

**变异算子**（AST 级，作用于非测试目录的 `.py`）：

| 算子 | 变换 |
|---|---|
| 比较运算符 | `<`↔`<=`，`>`↔`>=`，`==`↔`!=` |
| 算术运算符 | `+`↔`-`，`*`↔`//` |
| 布尔常量 | `True`↔`False` |
| 布尔运算符 | `and`↔`or` |
| 整数常量 | `n` → `n+1`（只认 `type(value) is int`） |

整数常量这一条有两处收窄，都写在 `mutate.py` 的模块 docstring 里：

- **只认 `type(value) is int`**，不是"数字"。`bool` 是 `int` 的子类，不排除的话 `True` 会走 `n → n+1` 被改成 `2`；浮点与复数不在算子表里。`True`↔`False` 由上一行的布尔常量那条负责。
- **只在几处位置上施加**：`Compare` / `BinOp` / `BoolOp` 的操作数，以及 `Return` / `Assign` 的右值。函数默认值（`def f(x=3)`）与 `AnnAssign`（`n: int = 7`）里的常量多半是接口契约，改一处会让一大片测试同时红，落不出"单点"的 ground truth。f-string 内部（整棵 `JoinedStr` 子树）也排除。

每次只施加**一处**变异——多处变异让 ground truth 不再是单点，也不像真实 bug。

**验证每个候选变异**：

1. 写入工作副本
2. 跑测试（范围见下）
3. 要求：产生**至少 1 个**新失败，且新失败数 `≤ max_new_failures`（默认 5）

第三条是关键：一个把套件炸掉一半的变异不是好任务——它太显眼，而且违反"单点缺陷"的前提。

**测试范围（`--scope`）**：

- `smart`（默认）：只跑与被变异文件**词干相关**的测试文件。源文件 `src/aifix/eval/mine.py` 的词干是 `mine`，候选测试文件是文件名含 `mine` 的那些（`tests/test_eval_mine.py`）。基线全量跑（前提确认那次）已经用 xunit1 的 `file` 属性给出了每个用例属于哪个测试文件，直接复用
- `full`：每个变异跑一次全量。小仓库上更准，大仓库上不可行

`smart` 会漏（被变异的文件其实由别处的测试覆盖），**漏是安全的**：丢弃这个候选变异，不产生假任务。生成器多试几个变异即可。

**任务落地**：`Task` 增加两个字段

```python
mutation_diff: str | None = None   # 施加在 base_commit 之上的变异补丁（unified diff）
origin: str = "mined"              # mined | mutated
```

`materialize` 在 checkout `base_commit`、嫁接测试之后、提交之前，若 `mutation_diff` 非空就 `git apply` 并一并提交。变异任务的 `commit == base_commit == HEAD`，`test_files == []`，`gold_files == [被变异的文件]`，`target_test` 取新失败集里的一个。

**为什么不直接在源仓库建提交**：那会污染用户的仓库。补丁随任务集走，任务集是一份自包含的 jsonl。

### 4.5 来源不混算

A 类（挖掘）与 C 类（变异）的分布不同，把它们的修复成功率平均成一个数字是错的。

`TaskResult` 增加 `origin`（从 `Task` 带过来）。`summarize_by_origin(results) -> list[Summary]`：任务集里出现多种来源时，按来源各出一行；单一来源时仍是一行。`Summary` 增加 `origin` 字段，对比表增加「来源」列。

---

## 5. A 组：补丁合理性静态信号

**边界**：只标注，不改判定。三态判定仍然只看测试结果。规格 §1 的「审查者是人」不变——本节做的是让那个人**有东西可看**。

**新模块 `src/aifix/signals.py`**，对每个被改动的 `.py` 文件做 AST 前后对比：

| 信号 | 判据 | 对应真实验收里的什么 |
|---|---|---|
| `removed_public_symbols` | 旧版本有、新版本没有的模块级 `def`/`class` 与类的公开方法，名字不以 `_` 开头 | 模型顺手删掉了无测试覆盖的 `mul` |
| `new_module_state` | 新版本新增的模块级赋值，右值是 `list`/`dict`/`set` 字面量或推导式，或对 `list()`/`dict()`/`set()` 的调用 | 模型把 `add` 改成有状态函数 |
| `files_outside_suspect` | 改动落在 Detector 报告的 `suspect_file` 之外的文件 | 改动面比诊断宽 |

三个信号都是**纯静态、零模型调用、确定性**的。

**"公开符号"的边界**：只看模块级函数/类，以及类里的方法（表示为 `Class.method`）。不看变量——`__all__` 之外的模块级常量重命名太常见，会淹没信号。

**接入点**：`verify_node`，在 commit / rollback 分支**之前**（这时改动还没提交，`git show HEAD:<path>` 拿得到旧内容）。`Worktree` 增加 `file_at_head(path) -> str | None`。

**输出**：

- `trace.fact("removed_public_symbol", 名字)`、`trace.fact("new_module_state", 名字)`、`trace.fact("files_outside_suspect", 列表)`
- 报告新增一节「⚠️ 值得多看一眼」，仅在有信号时出现；同时把「合并：`git merge …`」那行前面加一句提示
- `TaskResult` 增加 `signals: int`（信号条数合计），对比表增加「可疑信号」列

**这一列怎么读**（写进文档）：修复成功率高、可疑信号也高——那是规格套利的指纹。单看任何一列都得不出这个结论。

**明确的局限**：静态信号挡不住"在测试覆盖范围内把实现改成特例硬编码"。那需要覆盖率差分甚至语义分析，不在本里程碑。**这不是一个能靠加信号彻底解决的问题**——它是测试覆盖率作为天花板的直接后果。

---

## 6. 零散项

`eval/runner.py` 的跳过路径在**锁内**调用 `on_done`，正常路径与异常路径都在锁外。`on_done` 是用户提供的回调（CLI 里是 `print`），在锁内调用会把整批调度阻塞在一次 I/O 上，且行为与另外两条路径不一致。移到锁外。

---

## 7. 测试策略

**必须避免的两类断言**（本项目已有三次教训）：

1. **恒真断言**：`"0%" in "100%"` 永远为真；`assert cost > 0`；依赖终端宽度的帮助文本断言
2. **自造输入**：`test_violations.py` 九个用例全部自造匹配串，因而无法发现匹配串其实来自第三方

本里程碑的对应要求：

| 主题 | 断言必须是 |
|---|---|
| `make_test_id` | 拿**真跑一次 pytest 产出的 junit 报告**做输入，不是手写 XML 字符串；断言产出的 id **能被 pytest 真正跑起来** |
| xunit1 切换 | 一个哨兵测试直接对着真实报告断言 `file` 属性存在——pytest 哪天不写了就红 |
| Wilson 区间 | 对着已知的数值断言（`p̂=1, n=1` → 下界在 20%~22% 之间），不是断言"区间存在" |
| 变异生成器 | 断言产出的任务**确实红**（在 dest 处真跑一次），不是断言"生成了 N 条记录" |
| 静态信号 | 用真实验收里那个 case 的形状（删掉公开函数 + 新增模块级 dict）做输入，断言两个信号都命中 |
| 全量确认阶段 | 造一个"scoped 下红、全量下绿"的仓库，断言这个候选被排除 |

---

## 8. 不在本规格内

留给 M5：

- **`MavenAdapter`**（C 组）：验证适配层抽象是否真的成立
- **`aifix replay`**（E 组）：消费 `events.jsonl` 逐步重演
- **SQLite 跨 run 轨迹**（E 组）：jsonl 撑得住单 suite，跨 suite 跨时间的聚合仍缺一张表

留给第二阶段（规格 §13）：任务/issue 驱动、SWE-bench Lite / Defects4J、自动开 PR、第三个 `ProjectAdapter`。

**覆盖率差分**（A 组最贵的一档）没有里程碑归属——它需要先有 §4 的规模化数据，才知道值不值得。
