# M5「跨语言与可诊断」设计规格

**日期**：2026-07-28
**前置**：M1–M3b + M4 全部完成
**范围来源**：M3 计划「交给 M3b 的缺口」表里剩下的 C 组（`MavenAdapter`）与 E 组（`aifix replay`、SQLite 跨 run 轨迹）

---

## 1. 问题陈述

三件事，各自独立：

**其一，适配层抽象从没被验证过。** 规格给 `MavenAdapter` 的定位是「验证适配层抽象是否真的成立」。到 M4 为止只有 `PytestAdapter` 一个实现——一个只有单一实现的接口，无法区分「抽象对」和「抽象恰好长得像那一个实现」。

**其二，跑完一轮之后没法逐步复盘。** `events.jsonl` 已经在落盘（M2 就做了），但没有任何东西消费它。出问题时只能读原始 jsonl。

**其三，跨 run 的聚合缺一张表。** jsonl 撑得住单次 suite 的分析；「这个模型在最近十轮里的定位准确率趋势」「哪一条守卫触发得最频繁」这类问题，需要一张可查询的表。

---

## 2. 目标与非目标

**目标**

1. `MavenAdapter` 能在一个真实的 Maven 工程上跑通完整闭环，并且**把抽象的裂缝暴露出来、修掉**
2. `aifix replay` 让一次 run 可以逐步重看
3. 一张 SQLite 表把多次 run 的领域事实聚起来

**非目标**

- 不做 Java 的 `locate_source` 的深度优化（能从堆栈抽出仓库内帧即可）
- 不做 replay 的交互式 TUI。一次性输出，可 grep、可重定向
- 不做 SQLite 的查询前端。建表 + 灌数据 + 几条现成查询，够用
- **不做 Gradle**。规格 §13 把「第三个 `ProjectAdapter`」列在第二阶段

---

## 3. 已实测的 Maven 事实

以下都在本机跑出来过（Maven 3.9.10 / Java 21.0.3，`~/.m2` 已有 surefire 与 junit-jupiter 5.10.2，`mvn -o` 离线可用）。

**命令**

```
mvn -B -q -o clean test -Dmaven.test.failure.ignore=true
```

`-Dmaven.test.failure.ignore=true` 必须加：否则测试一红 `mvn` 就以非 0 退出，而**报告已经写出来了**——不加的话调用方容易把退出码当成「没跑成」。

`clean` 必须加，理由是**正确性不是整洁**：`mvn test` 不清空 `target/surefire-reports/`，而 `report_paths` 只看文件系统当前状态。跑一次全量留下 A、B、C，再跑只测 A 的复跑，目录里仍躺着上一轮的 B、C，`parse_junit` 会把上一轮的失败算成这一轮的——flaky 确认据此判定，不报错，只是判错。`run_full_suite` / `run_scoped` 的 `finally` 里确实会删报告，但只删 `report_paths` 当时返回的那些，任何一次异常退出都会留下残骸；把这件事挂在别人的 `finally` 上不成立。代价是每次重新编译（本机约 3 秒）。

**报告位置：是 glob，不是单个文件**

```
target/surefire-reports/TEST-demo.CalcTest.xml     ← 每个测试类一份
target/surefire-reports/demo.CalcTest.txt
```

**这正是适配层抽象的裂缝**，见 §4。

**XML 形状**

- 根元素是 `<testsuite>`（pytest 是 `<testsuites>`）。`parse_junit` 用 `root.iter("testcase")`，两者都走得通，不用改
- `<testcase classname="demo.CalcTest" name="addWorks" time="0.027">`——**没有 `file` 属性**（和 pytest 的 xunit2 一样）
- `<failure message="expected: <3> but was: <-1>" type="org.opentest4j.AssertionFailedError">`，text 是完整 Java 堆栈

---

## 4. 裂缝：`report_glob()` 返回单个字符串

`ProjectAdapter` 现在的形状是 `report_glob() -> str`，而 `run_full_suite` / `run_scoped` 都写死

```python
return parse_junit([worktree / report], adapter.make_test_id)
```

一个路径。Maven 要多个。

**修法**：把接口改成

```python
def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]: ...
```

`report_glob()` **整个删掉**，不保留任何「默认实现对它做一次 glob」的过渡形态：留着它等于让协议同时有两个回答报告位置的成员，而 Maven 只能回答其中一个。`parse_junit` 本来就接受 `Iterable[Path]`，不用改。

`scoped` 参数是决定性的，不是可选的排版细节：`run_scoped` 用的报告名必须与全量那份**不同**（pytest 侧是 `.aifix-recheck.xml`），否则 flaky 复跑会覆盖掉还要继续用的全量报告。Maven 侧两者由 surefire 写在同一个目录，忽略这个参数即可（命令里的 `clean` 保证目录里只有本次跑出来的）。

`run_full_suite` / `run_scoped` 里「跑完删报告」那一步同样要跟着改成删多个。

**这一改动是本里程碑的主要收获**：它证明了抽象**不对**，也证明了这个验证有价值——如果 `MavenAdapter` 顺顺当当地接上了，那才说明这个里程碑白做了。

**另一处裂缝**：`split_paths` 的 `test_dirs` 判据。M4 已经把它从「第一段相等」改成了「路径分段前缀匹配」，正是为这里准备的——Maven 标准布局是 `src/test/java/...`，`test_dirs` 会是 `["src/test"]`，第一段是 `src`。M4 那次改动是预防性的，M5 是它的第一个真实消费者。

---

## 5. `MavenAdapter`

| 成员 | 实现 |
|---|---|
| `name` | `"maven"` |
| `detect(repo)` | `(repo / "pom.xml").is_file()` |
| `full_test_command()` | `["mvn", "-B", "-q", "-o", "clean", "test", "-Dmaven.test.failure.ignore=true"]` |
| `scoped_test_command(ids)` | 追加 `-Dtest=<用逗号连接的 id>` 与 `-DfailIfNoSpecifiedTests=false` |
| `report_paths(worktree, scoped=False)` | `sorted(worktree.glob("target/surefire-reports/TEST-*.xml"))`，忽略 `scoped` |
| `test_dirs()` | `["src/test"]` |
| `source_suffixes()` | `(".java",)` |
| `make_test_id(classname, name, file)` | `f"{classname}#{name}"`（surefire 的 `-Dtest=` 语法） |
| `locate_source(failure, repo)` | 见下 |

两个成员的签名与上面 §4 保持一致：命令**不接收报告路径**（表头里也不许再写 `full_test_command(report)`），报告位置由 `report_paths()` 单独回答。

**`source_suffixes()` 是本里程碑新增的协议成员**。挖任务时「哪些后缀算源文件」的判据必须由适配器给，不能写死在挖掘代码里：`eval/mine.split_paths` 曾经只认 `.py`，于是 Java 仓库的源码全部落空 → `gold_files` 恒空 → `is_candidate` 恒 `False` → `aifix mine` 对任何 Maven 工程产出 0 个任务，且不报错，与「这个仓库最近没有红转绿的提交」无法区分。Maven 侧只收 `.java`：`pom.xml` 的改动确实能让测试转红转绿，但它不是 `locate_source` 能指向的东西，塞进 `gold_files` 等于给 Detector 记一个它按设计就拿不到的分。

**`report_path` 参数怎么办**：`full_test_command(report_path)` 现在把报告路径传给适配器。Maven 不接受这个参数——报告位置由 surefire 决定。**接口要跟着变**：命令不再接收报告路径，报告位置由 `report_paths()` 单独回答。pytest 侧把 `--junitxml=` 的路径变成适配器自己的常量。

**`-DfailIfNoSpecifiedTests=false` 必须加**：类名对不上时不至于让整个构建失败。

**`locate_source`**：Java 栈帧形如 `\tat demo.Calc.add(Calc.java:3)`。要抽出「类的全限定名 + 文件名 + 行号」，再映射回 `src/main/java/<包路径>/<文件名>`。栈顶是断言框架自己的帧（`org.junit.jupiter.api.AssertionFailureBuilder.build`），按包名前缀过滤掉 `org.junit` / `org.opentest4j` / `java.` / `jdk.` / `sun.`。

**验收必须是真跑**：一个最小 Maven 工程（`Calc.add` 故意写成 `a - b`，`CalcTest.addWorks` 断言 `3`），跑完整的 `aifix run`，红测试进、绿分支出。本机 `mvn -o` 可用，不依赖网络。

---

## 6. `aifix replay`

**输入**：`.aifix/runs/<run_id>/`（`events.jsonl` + `facts.jsonl`）。
**输出**：一次性文本，按时间顺序，可 grep、可重定向。

```
aifix replay <run_id> [--repo .] [--step N] [--full]
```

每一步渲染：步号、事件类型、模型说了什么、调了什么工具带什么参数、工具返回了什么（默认截断，`--full` 不截断）、这一步的 token 与成本。领域事实（`facts.jsonl` 里的 verdict / rollback / guard 命中 / 信号）按其所属的 failure 与 attempt 插进对应位置。

**为什么不做交互式**：这是一个诊断工具，最常见的用法是「跑一遍、翻到出问题那一步、把那几行贴给别人看」。一次性输出满足它，交互式反而挡路。

**`--step N`**：只渲染第 N 步。定位到问题之后要反复细看某一步时用。

---

## 7. SQLite 跨 run 轨迹

**表**：`.aifix/trajectory.db`（仓库级，不是 run 级）。

```sql
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT, adapter TEXT, branch TEXT,
    baseline_failures INTEGER, fixed INTEGER,
    spent_tokens INTEGER, spent_usd REAL,
    abort TEXT, abort_kind TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    run_id TEXT, failure TEXT, attempt INTEGER,
    key TEXT, value TEXT
);
CREATE INDEX IF NOT EXISTS facts_key ON facts(key);
```

**灌数据**：`aifix ingest [--repo .]` 扫 `.aifix/runs/*/facts.jsonl` 与 `report.md` 落库。**不在 run 结束时自动落库**——那会给核心循环加一个可能失败的写路径，而这个功能是诊断用的，事后灌就够了。幂等（`INSERT OR REPLACE` on `run_id`，facts 按 run_id 先删后插）。

**现成查询**：`aifix stats [--repo .]` 输出几条：每个 adapter 的 run 数与修复数、守卫触发次数按种类排序、可疑信号出现最多的 run。

**为什么不直接用 jsonl + grep**：单次 suite 可以。跨 run 要的是「按 key 聚合、按时间排序、按 run 关联」——那是 SQL 干的事，用 grep 拼出来的东西不可信也不可复用。

---

## 8. 顺序与依赖

1. **接口重构**（`report_paths`、命令不再接收报告路径）——必须先做，`MavenAdapter` 依赖它，且它会碰 `run_full_suite` / `run_scoped` 这两个被处处调用的函数
2. **`MavenAdapter`** + 真实 Maven 工程端到端验收
3. **`aifix replay`**——独立，不依赖 1/2
4. **SQLite 轨迹**——独立

3 和 4 可以与 2 并行。

---

## 9. 测试策略

沿用 M4 的两条硬要求：

1. **不自造第三方的产出**。断言 surefire 报告形状的测试必须**真跑一次 `mvn`**（本机 `mvn -o` 可用）。手写的 XML 只能证明我们理解得自洽。
2. **断言必须有区分度**。本项目已有五次以上恒真断言教训。

本里程碑的具体要求：

| 主题 | 断言必须是 |
|---|---|
| `report_paths` 重构 | 一条测试断言 pytest 侧行为**逐点不变**（同一个仓库，重构前后失败集相同） |
| `MavenAdapter` | 真跑 `mvn`，断言解析出的 id **能被 `-Dtest=` 真正跑起来**（跑完看 surefire 报告里 `tests` 计数） |
| Java `locate_source` | 断言框架自身的帧（`org.junit.*`）**必须被过滤掉**——只断言「抽出了帧」会让一个只返回栈顶的实现也通过 |
| `replay` | 用一份真实的 `events.jsonl`（跑一次 scripted run 生成），断言每一步的工具调用与结果都出现了，且 `--step N` 只出一步 |
| SQLite | 断言**幂等**：同一个 run 灌两次，`facts` 的行数不变（不是「灌进去了」） |

---

## 10. 不在本规格内

- **Gradle / Go / Jest 适配器**：规格 §13 的第二阶段
- **覆盖率差分**（A 组最贵的一档）：需要先有 M4 规模化数据才知道值不值得
- **任务/issue 驱动**、SWE-bench Lite / Defects4J、自动开 PR：规格 §13 的第二阶段
- **跑那一轮花钱的完整跨模型评测**：外向动作，由用户决定
