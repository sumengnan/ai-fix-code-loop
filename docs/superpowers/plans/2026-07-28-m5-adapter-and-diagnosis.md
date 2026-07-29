# M5「跨语言与可诊断」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development 逐任务实现此计划。

**目标：** 用一个真实的 `MavenAdapter` 验证适配层抽象是否成立（并修掉它暴露的裂缝）；`aifix replay` 让一次 run 可以逐步复盘；一张 SQLite 表把多次 run 的领域事实聚起来。

**架构：** 三条互相独立的线。第一条要先动 `ProjectAdapter` 的接口（报告位置从「单个路径」变成「一组路径」），这是本里程碑的主要收获——抽象在这里露出裂缝，恰恰证明这次验证有价值。

**技术栈：** Python 3.14 · Maven 3.9.10 / Java 21（本机可用，`mvn -o` 离线可跑）· sqlite3 标准库 · uv

**规格：** `docs/superpowers/specs/2026-07-28-m5-adapter-and-diagnosis-design.md`

---

## 全局约束

1. **提交署名**：一律 `git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "..." -- <paths>`。**必须带 pathspec**——裸 `git commit` 会提交整个 index，并发时会卷进别人的文件（本项目发生过一次）。commit message 中**绝不出现** AI / Claude / Anthropic / `Co-Authored-By` 字样，包括 `Co-Authored-By: sumengnan`。
2. **严格 TDD**：先写失败测试 → 跑一次确认失败并把真实输出记进报告 → 实现 → 跑通 → commit。
3. **断言必须有区分度**。本项目已有**六次以上**恒真断言教训：`"0%" in "100%"` 恒真；`assert cost > 0`；帮助文本随终端宽度飘；整体替换被 patch 的函数导致代码提前返回、断言恒真却一直绿；写死下标的断言插列后错位却照样绿；`for x in empty: assert ...` 无条件通过。
4. **不自造第三方的产出**。断言 surefire / junit 报告形状的测试必须**真跑一次 `mvn` 或 `pytest`**。手写的 XML 只能证明我们理解得自洽，证明不了工具真的这么写。
5. 注释写「为什么」不写「是什么」，中文。
6. 全量套件约 300 秒（387 项）。只在任务要求时跑。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/aifix/adapters/base.py` | 适配器协议 | 改：`report_glob() -> str` 换成 `report_paths(worktree) -> list[Path]`；测试命令不再接收报告路径 |
| `src/aifix/adapters/pytest_adapter.py` | pytest 适配 | 改：跟着接口走 |
| `src/aifix/adapters/maven_adapter.py` | **新建** | `MavenAdapter` |
| `src/aifix/nodes/baseline.py` | 跑测试 | 改：`run_full_suite` / `run_scoped` 消费多个报告 |
| `src/aifix/replay.py` | **新建** | 消费 `events.jsonl` + `facts.jsonl` 渲染逐步复盘 |
| `src/aifix/trajectory.py` | **新建** | SQLite 建表、灌数据、现成查询 |
| `src/aifix/cli.py` | 命令行 | 改：加 `replay` / `ingest` / `stats` 子命令 |

---

## 阶段一 · 适配层抽象（C 组）

### 任务 1：报告位置从「一个路径」变成「一组路径」

**背景**：`ProjectAdapter.report_glob()` 返回单个字符串，`run_full_suite` / `run_scoped` 写死 `parse_junit([worktree / report], ...)`。**已实测**：Maven surefire 把报告写成 `target/surefire-reports/TEST-<每个测试类>.xml`，**每个测试类一份**。这就是抽象的裂缝。

同一处还有第二个裂缝：`full_test_command(report_path)` 把报告路径**传给**适配器。Maven 不接受这个参数——报告位置由 surefire 决定，不由调用方指定。

**文件：** 改 `src/aifix/adapters/base.py`、`src/aifix/adapters/pytest_adapter.py`、`src/aifix/nodes/baseline.py`；测试 `tests/test_pytest_adapter.py`、`tests/test_nodes_preflight_baseline.py`

- [ ] **步骤 1：写失败测试**

```python
def test_report_paths_returns_a_list(tmp_path):
    """pytest 只有一份报告，但接口必须是列表 —— Maven 有多份。"""
    a = PytestAdapter()
    (tmp_path / a.REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
    assert a.report_paths(tmp_path) == [tmp_path / a.REPORT_NAME]


def test_report_paths_is_empty_when_nothing_was_written(tmp_path):
    """报告缺失返回空列表，不是抛 —— require_report 那一层才负责判断。"""
    assert PytestAdapter().report_paths(tmp_path) == []


def test_commands_no_longer_take_a_report_path():
    """报告位置是适配器的属性，不是调用方的参数。"""
    a = PytestAdapter()
    cmd = a.full_test_command()
    assert any(x.startswith("--junitxml=") for x in cmd)
```

外加一条**行为不变**的回归测试：同一个仓库，改造前后 `run_full_suite` 得到的失败集**逐点相同**。用一个真实的小仓库夹具跑（`tests/conftest.py` 里有现成的），把改造前的失败集**写死在测试里**当基准。

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

`base.py` 的 `ProjectAdapter` 协议：

```python
    def full_test_command(self) -> list[str]: ...
    def scoped_test_command(self, test_ids: list[str]) -> list[str]: ...
    def report_paths(self, worktree: Path,
                     scoped: bool = False) -> list[Path]: ...
    def source_suffixes(self) -> tuple[str, ...]: ...
```

`PytestAdapter` 增加 `REPORT_NAME = ".aifix-report.xml"` 与 `SCOPED_REPORT_NAME = ".aifix-recheck.xml"`。

**注意**：`run_scoped` 现在用的是**另一个**报告名（`.aifix-recheck.xml`），为的是不覆盖全量那份。接口改造后这个区分要保留——`report_paths` 需要能分别回答两者，或者由调用方传一个「哪一份」的标识。

**已选定：由调用方传标识**，即 `report_paths(worktree, scoped=False)`。这个参数是决定性的，不是可选的排版细节——pytest 侧全量与复跑必须落在两份不同的文件上，否则 flaky 复跑会覆盖掉还要继续用的全量报告。Maven 侧两者是同一个目录，忽略这个参数即可。

**新增协议成员 `source_suffixes()`**：挖任务时「哪些后缀算源文件」的判据必须由适配器给。`eval/mine.split_paths` 曾经只认 `.py`，于是 Java 仓库的源码全部落空 → `gold_files` 恒空 → `is_candidate` 恒 `False` → `aifix mine` 对任何 Maven 工程产出 0 个任务，且不报错，与「这个仓库最近没有红转绿的提交」无法区分。

`baseline.py`：

```python
        await sb.exec(adapter.full_test_command(), timeout)
        paths = adapter.report_paths(worktree)
        _check_report(worktree, paths, require_report)
        return parse_junit(paths, adapter.make_test_id)
```

`_check_report` 的判据从「那个文件在不在」变成「这一组是不是空的」。**它的报错消息要跟着改**——现在的消息里写着具体的文件名，多报告场景下那句话会变成假话。

跑完删报告那一步同样要删多个。

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_pytest_adapter.py tests/test_nodes_preflight_baseline.py tests/test_flaky.py tests/test_tool_runtests.py tests/test_junit.py -q`

- [ ] **步骤 5：跑全量套件**

这次改动碰的是被处处调用的函数，**必须跑全量**：`uv run pytest -q` → 预期 387 + 新增，全绿。

- [ ] **步骤 6：Commit**

---

### 任务 2：`MavenAdapter`

**已实测的 Maven 事实（照抄，不要自己再试一遍）：**

- 命令：`mvn -B -q -o clean test -Dmaven.test.failure.ignore=true`。`clean` **必须加**，理由是正确性不是整洁：`mvn test` 不清空 `target/surefire-reports/`，而 `report_paths` 只看文件系统当前状态——上一轮留下的报告会被 `parse_junit` 当成这一轮的结果，flaky 确认据此判定，不报错，只是判错。`-Dmaven.test.failure.ignore=true` **必须加**：否则测试一红 `mvn` 就以非 0 退出，而**报告已经写出来了**，调用方容易把退出码当成「没跑成」
- 报告：`target/surefire-reports/TEST-<FQCN>.xml`，每个测试类一份
- 根元素是 `<testsuite>`（pytest 是 `<testsuites>`）。`parse_junit` 用 `root.iter("testcase")`，两者都走得通，不用改
- `<testcase classname="demo.CalcTest" name="addWorks" time="0.027">`——**没有 `file` 属性**
- `<failure message="expected: <3> but was: <-1>" type="org.opentest4j.AssertionFailedError">`，text 是完整 Java 堆栈
- 本机 `~/.m2` 已有 surefire 与 junit-jupiter 5.10.2，`-o`（离线）可用，**不需要网络**

**文件：** 创建 `src/aifix/adapters/maven_adapter.py`、`tests/test_maven_adapter.py`；改 `src/aifix/nodes/baseline.py` 的 `_ADAPTERS`

- [ ] **步骤 1：写失败测试**

夹具：一个最小 Maven 工程（`pom.xml` + `src/main/java/demo/Calc.java` 里 `add` 故意写成 `a - b` + `src/test/java/demo/CalcTest.java` 断言 `3`）。

```python
def test_parses_a_real_surefire_report(maven_repo):
    """真跑一次 mvn —— 手写 XML 只能证明我们理解得自洽。"""
    a = MavenAdapter()
    subprocess.run(a.full_test_command(), cwd=maven_repo, capture_output=True)
    paths = a.report_paths(maven_repo)
    assert paths, "surefire 没产出报告"
    fs = parse_junit(paths, a.make_test_id)
    assert "demo.CalcTest#addWorks" in fs.ids
    assert "demo.CalcTest#alsoPasses" in fs.ran
    assert "demo.CalcTest#alsoPasses" not in fs.ids


def test_scoped_id_is_runnable(maven_repo):
    """合成的 id 必须能被 -Dtest= 真正跑起来。"""
    a = MavenAdapter()
    subprocess.run(a.scoped_test_command(["demo.CalcTest#addWorks"]),
                   cwd=maven_repo, capture_output=True)
    root = ET.parse(a.report_paths(maven_repo)[0]).getroot()
    assert root.get("tests") == "1", dict(root.attrib)


def test_locate_source_filters_out_the_assertion_framework(maven_repo):
    """栈顶是 org.junit.* 的帧 —— 只断言「抽出了帧」会让一个只返回栈顶的实现也通过。"""
    # 跑一次拿到真实的 Failure，断言候选里没有 org.junit / org.opentest4j 的帧，
    # 且第一个候选指向 src/main/java/demo/Calc.java
```

- [ ] **步骤 2：跑测试确认失败**（模块不存在）

- [ ] **步骤 3：实现**

| 成员 | 实现 |
|---|---|
| `name` | `"maven"` |
| `detect(repo)` | `(repo / "pom.xml").is_file()` |
| `full_test_command()` | `["mvn", "-B", "-q", "-o", "clean", "test", "-Dmaven.test.failure.ignore=true"]` |
| `scoped_test_command(ids)` | 追加 `-Dtest=<逗号连接>` 与 `-DfailIfNoSpecifiedTests=false` |
| `report_paths(worktree, scoped=False)` | `sorted(worktree.glob("target/surefire-reports/TEST-*.xml"))`，忽略 `scoped` |
| `test_dirs()` | `["src/test"]` |
| `source_suffixes()` | `(".java",)`——只收产品代码；`pom.xml` 不进 `gold_files`，它不是 `locate_source` 能指向的东西 |
| `make_test_id(cn, name, file)` | `f"{cn}#{name}"` |

`-DfailIfNoSpecifiedTests=false` **必须加**：类名对不上时不至于让整个构建失败。

`locate_source`：Java 栈帧形如 `\tat demo.Calc.add(Calc.java:3)`。抽出「全限定类名 + 文件名 + 行号」，映射回 `src/main/java/<包路径>/<文件名>`。按包名前缀过滤掉 `org.junit` / `org.opentest4j` / `java.` / `jdk.` / `sun.`。栈由浅入深，**最深的排最前**（与 `PytestAdapter` 一致）。

`_ADAPTERS` 里注册 `"maven": MavenAdapter`。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 3：Maven 端到端验收

**这是一个验证任务。**

- [ ] 用任务 2 的夹具工程建一个 git 仓库，跑完整的 `aifix run`（scripted 假客户端，让它打一个把 `a - b` 改成 `a + b` 的补丁）
- [ ] 断言：红测试进、绿分支出，主工作区未被触碰
- [ ] **同时验证 `split_paths` 对 `src/test/java/...` 的判定**——这是 M4 那次分段前缀改动的第一个真实消费者
- [ ] **同时验证 `tools/patch.py` 的「不许改测试文件」守卫**对 `src/test/java/demo/CalcTest.java` 生效（M4 修的就是这一条，现在有了真实场景）
- [ ] 把实测结果写进报告文件

---

## 阶段二 · `aifix replay`

### 任务 4：`replay.py`

**文件：** 创建 `src/aifix/replay.py`、`tests/test_replay.py`

**输入**：`.aifix/runs/<run_id>/`（`events.jsonl` + `facts.jsonl`）。
**输出**：一次性文本，按时间顺序，可 grep、可重定向。**不做交互式**——最常见的用法是「跑一遍、翻到出问题那一步、把那几行贴给别人看」。

- [ ] **步骤 1：写失败测试**

```python
async def test_replay_shows_every_tool_call_and_result(tmp_path):
    """用一份**真跑出来**的 events.jsonl —— 不手写事件。"""
    # 用 scripted client 跑一次 run_once，拿到 .aifix/runs/<id>/
    # 断言：每一次工具调用的名字与参数都出现了，工具返回也出现了
    # 断言：facts 里的 verdict 出现在对应位置


def test_step_selects_exactly_one_step(...):
    out_all = render(run_dir)
    out_one = render(run_dir, step=2)
    assert out_one.count("步骤") == 1
    assert len(out_one) < len(out_all)


def test_truncation_is_marked_and_full_disables_it(...):
    """截断必须**看得出来**被截断了 —— 悄悄截断是这个项目最忌讳的形状。"""
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

```python
def render(run_dir: Path, step: int | None = None,
           full: bool = False, max_chars: int = 2000) -> str:
```

每一步渲染：步号、事件类型、模型说了什么、调了什么工具带什么参数、工具返回了什么（默认截断并**标注截断**，`full=True` 不截断）、这一步的 token 与成本。领域事实按其所属的 failure 与 attempt 插进对应位置。

事件用 `harness.persistence.serialize` 反序列化——`trace.record_events` 就是用 `event_to_dict` 写的，读回来要用对应的那一侧。**先去确认那个模块导出了什么**，别假设有 `dict_to_event`；没有的话就按 `type` 字段自己分发，并在注释里写明为什么不用框架的反序列化。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 5：`aifix replay` 子命令

**文件：** 改 `src/aifix/cli.py`、`tests/test_cli_args.py`

```
aifix replay <run_id> [--repo .] [--step N] [--full]
```

帮助文本断言**必须**走 `tests/test_cli_args.py` 已有的 `_sub_help(name)`（内部已剥 ANSI 并删掉全部空白）。中文帮助文本没有词间空格，`textwrap` 在任意位置硬断，任何保留空白的比对都会随终端宽度飘——本项目 `COLUMNS=45` 当场红过一次。

run_id 不存在时给一句人话，并**列出这个仓库里有哪些 run**（诊断工具的第一要务是让人找得到东西）。

---

## 阶段三 · SQLite 跨 run 轨迹

### 任务 6：`trajectory.py`

**文件：** 创建 `src/aifix/trajectory.py`、`tests/test_trajectory.py`

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

`ingest(repo) -> int`：扫 `.aifix/runs/*/facts.jsonl` 落库，返回灌进去的 run 数。**幂等**：`INSERT OR REPLACE` on `run_id`，facts 按 run_id **先删后插**。

**不在 run 结束时自动落库**——那会给核心循环加一个可能失败的写路径，而这个功能是诊断用的，事后灌就够了。把这个理由写进模块 docstring。

- [ ] **失败测试的核心一条**：**幂等**。同一个 run 灌两次，`facts` 的行数**不变**。断言「灌进去了」是没有区分度的——第二次灌进去翻倍才是这个功能最可能的 bug。

`query_stats(db) -> dict`：每个 adapter 的 run 数与修复数、守卫触发次数按种类排序、可疑信号出现最多的 run。

---

### 任务 7：`aifix ingest` / `aifix stats` 子命令

**文件：** 改 `src/aifix/cli.py`、`tests/test_cli_args.py`

同任务 5 的帮助文本要求。

---

### 任务 8：整体验收

- [ ] 全量套件全绿
- [ ] 真跑一次 `aifix replay`（用阶段一/二产生的真实 run 目录），把输出片段写进报告
- [ ] 真跑 `aifix ingest` + `aifix stats`，确认幂等（连灌两次，行数不变）
- [ ] 把实测数字写进报告

---

## 交给后续

- **Gradle / Go / Jest 适配器**：规格 §13 的第二阶段
- **覆盖率差分**（A 组最贵的一档）
- **任务/issue 驱动**、SWE-bench Lite / Defects4J、自动开 PR
- **跑那一轮花钱的完整跨模型评测**：外向动作，由用户决定
