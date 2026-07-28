# M4「有结论」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 把跨模型对比表从「1 个样本的形状演示」变成「有结论的度量」，修掉挡在路上的适配层缺陷，并给规格套利装上零模型调用的静态信号。

**架构：** 六条独立的改动线，共用现有的 `Task` / `TaskResult` / `FailureSet` 骨架。挖掘从「两次全量」改成「两次 scoped + 一次全量确认」；`make_test_id` 补齐类内测试与收集错误两种形状；对比表加 Wilson 区间与分数；新增 `signals.py`（AST 静态信号）与 `mutate.py`（人造变异）。

**技术栈：** Python 3.14 · pydantic v2 · pytest · `ast` 标准库 · uv

**规格：** `docs/superpowers/specs/2026-07-28-m4-conclusive-design.md`

---

## 全局约束

**每一个实现子代理都必须遵守：**

1. **提交署名**：一律 `git -c user.name=sumengnan -c user.email=2499165351@qq.com commit`。commit message 中**绝不出现** AI / Claude / Anthropic / `Co-Authored-By` 字样（`Co-Authored-By: sumengnan` 也不行——字面命中禁止清单）。
2. **严格 TDD**：先写失败测试 → 跑一次确认失败 → 实现 → 跑通 → commit。
3. **测试命令**：`uv run pytest tests/<file> -q`。本仓库全量套件约 171 秒，只在任务要求时跑全量。
4. **断言必须有区分度**。本项目已有三次「恒真断言」教训：
   - `"0%" in "100%"` 永远为真
   - `assert cost > 0`
   - 帮助文本断言随终端宽度飘（`COLUMNS=45` 当场红）
   处理办法：断言**具体数值**或**具体字符串等值**；涉及帮助文本时先剥 ANSI 再删掉全部空白再比对。
5. **不要自造第三方的产出**。凡是断言 pytest / junit 产物形状的测试，必须**真跑一次 pytest** 拿到报告，不能手写 XML 字符串——手写的 XML 只能证明我们理解得自洽，证明不了 pytest 真的这么写。
6. 注释写「为什么」，不写「是什么」。中文。

**关于本计划里的测试代码**：需要真实 git 仓库夹具的几处（任务 3 / 9 / 13 / 15），计划给的是**断言目标与判据**而非逐字代码——因为本仓库已经有构造仓库的辅助函数，逐字写一份新的只会造出第二套。这几处请先读对应测试文件里的既有写法并复用。**这不是"自由发挥"的许可**：断言目标、具体数值、必须区分开的两种情形都写死在计划里了，照着实现，别削弱。

---

## 文件结构

| 文件 | 职责 | 本计划的动作 |
|---|---|---|
| `src/aifix/adapters/pytest_adapter.py` | pytest 适配 | 改：`_BASE` 加 xunit1；`make_test_id` 重写 |
| `src/aifix/eval/mine.py` | 从 git history 挖任务 | 改：`split_paths` 收非 `.py`；`verify_commit` 四阶段 |
| `src/aifix/eval/score.py` | 打分与对比表 | 改：Wilson 区间、分数、来源列、信号列 |
| `src/aifix/eval/stats.py` | **新建** | Wilson score interval，纯函数 |
| `src/aifix/eval/mutate.py` | **新建** | AST 变异算子 + 变异任务生成 |
| `src/aifix/eval/task.py` | 数据模型 | 改：`Task` 加 `mutation_diff`/`origin`；`TaskResult` 加 `origin`/`signals` |
| `src/aifix/eval/workspace.py` | 任务落地 | 改：`materialize` 施加 `mutation_diff` |
| `src/aifix/eval/runner.py` | 并行调度 | 改：`on_done` 移出锁；`TaskResult` 带 `origin`/`signals` |
| `src/aifix/signals.py` | **新建** | 补丁合理性静态信号（AST 前后对比） |
| `src/aifix/delivery.py` | worktree | 改：加 `file_at_head` |
| `src/aifix/nodes/verify.py` | 三态判定 | 改：commit/rollback 前算信号 |
| `src/aifix/nodes/report.py` | 报告渲染 | 改：加「值得多看一眼」一节 |
| `src/aifix/graph.py` | 状态 | 改：`AifixState` 加 `signals` |
| `src/aifix/cli.py` | 命令行 | 改：加 `mutate` 子命令 |

---

## 阶段一 · 适配层（C 组）

### 任务 1：`make_test_id` 补齐类内测试与收集错误

**背景**：pytest 默认 `junit_family=xunit2` 不写 `<testcase file=...>`。当前实现走 `classname.replace(".", "/") + ".py"` 回退路径，对类内测试拼出不存在的路径。一个无效 id 进 pytest 命令行会让 pytest 在收集阶段整轮中止（exit 4），写出 `tests="0"` 的空报告——报告存在，`require_report` 查不出异常，看起来像"全部复跑通过"。

**文件：**
- 修改：`src/aifix/adapters/pytest_adapter.py`
- 测试：`tests/test_pytest_adapter.py`

- [ ] **步骤 1：写失败测试**

追加到 `tests/test_pytest_adapter.py`。**注意这些测试真跑 pytest**，不手写 XML：

```python
import subprocess, sys
import xml.etree.ElementTree as ET
from aifix.adapters.junit import parse_junit

_SAMPLE = '''
import pytest

def test_top_fails():
    assert 1 == 2

class TestBar:
    def test_in_class_fails(self):
        assert 1 == 2
    def test_in_class_ok(self):
        pass

@pytest.mark.skip(reason="故意跳过")
def test_skipped():
    pass
'''


def _run_pytest(cwd, args):
    return subprocess.run([sys.executable, *args], cwd=cwd,
                          capture_output=True, text=True)


def test_junit_report_carries_file_attribute(tmp_path):
    """哨兵：适配器依赖 <testcase file=...> 存在。pytest 哪天不写了就红。

    不手写 XML —— 手写的只能证明我们理解得自洽，证明不了 pytest 真这么写。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_s.py").write_text(_SAMPLE, encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command("r.xml"))
    cases = list(ET.parse(tmp_path / "r.xml").getroot().iter("testcase"))
    assert cases, "pytest 没产出任何 testcase"
    assert all(c.get("file") for c in cases), \
        f"有 testcase 缺 file 属性：{[dict(c.attrib) for c in cases]}"


def test_class_based_test_id_is_runnable(tmp_path):
    """类内测试合成出的 id 必须能被 pytest 真正跑起来。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_s.py").write_text(_SAMPLE, encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command("r.xml"))
    fs = parse_junit([tmp_path / "r.xml"], a.make_test_id)
    tid = "tests/test_s.py::TestBar::test_in_class_fails"
    assert tid in fs.ids, f"合成的 id 不对：{sorted(fs.ids)}"
    # 真跑一次：无效 id 会让 pytest 在收集阶段整轮中止
    res = _run_pytest(tmp_path, a.scoped_test_command([tid], "r2.xml"))
    root = ET.parse(tmp_path / "r2.xml").getroot()
    suite = next(root.iter("testsuite"))
    assert suite.get("tests") == "1", \
        f"pytest 没跑到这个用例：{dict(suite.attrib)}\n{res.stdout}"


def test_collection_error_id_is_the_file_path(tmp_path):
    """收集错误：classname 为空、name 是点分模块名，id 必须退回文件路径。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text(
        "from nonexistent_module import thing\n"
        "def test_x(): assert thing()\n", encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command("r.xml"))
    fs = parse_junit([tmp_path / "r.xml"], a.make_test_id)
    assert fs.ids == {"tests/test_broken.py"}, sorted(fs.ids)
    # 这个 id 必须可重跑
    res = _run_pytest(tmp_path,
                      a.scoped_test_command(["tests/test_broken.py"], "r2.xml"))
    assert "ERROR" in res.stdout or "error" in res.stdout.lower()
    assert (tmp_path / "r2.xml").is_file()


def test_make_test_id_without_file_strips_class_segments():
    """回退路径（file 缺失，如别的适配器）：尾部大写段当类名，不整段替换。"""
    a = PytestAdapter()
    assert a.make_test_id("tests.test_foo", "test_top", None) == \
        "tests/test_foo.py::test_top"
    assert a.make_test_id("tests.test_foo.TestBar", "test_baz", None) == \
        "tests/test_foo.py::TestBar::test_baz"
    assert a.make_test_id("tests.test_foo.TestOuter.TestInner", "t", None) == \
        "tests/test_foo.py::TestOuter::TestInner::t"
```

- [ ] **步骤 2：跑测试确认失败**

`uv run pytest tests/test_pytest_adapter.py -q`
预期：`test_junit_report_carries_file_attribute` 失败（xunit2 不写 file）；
`test_class_based_test_id_is_runnable` 失败（id 是 `tests/test_s.py::test_in_class_fails`，`tests="0"`）；
`test_collection_error_id_is_the_file_path` 失败（id 是 `tests/test_broken.py::tests.test_broken`）；
`test_make_test_id_without_file_strips_class_segments` 失败（第二、三条产出 `tests/test_foo/TestBar.py::test_baz`）。

- [ ] **步骤 3：实现**

`src/aifix/adapters/pytest_adapter.py`：

```python
from pathlib import Path, PurePosixPath

    # -o junit_family=xunit1：xunit2（pytest 的默认）**不写** <testcase file=...>，
    # 而 file 是把 junit 报告里的用例还原成可重跑 node id 的唯一可靠依据。
    # 已实测（pytest 9.1.1）：xunit1 多出 file/line 两个属性，其余结构
    # （skipped / failure / error / message）与 xunit2 完全一致，且无
    # deprecation 警告。`-o` 覆盖目标项目 ini 里的设置。
    _BASE = ["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "junit_family=xunit1"]

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """把 junit 报告里的一条 <testcase> 还原成 pytest 认得的 node id。

        三种形状，来源都是实测：

        1. 收集错误 —— classname="" / name="tests.test_x" / file="tests/test_x.py"。
           整个文件没能导入，pytest 发的是一条文件级 <error>。此时可重跑的
           node id 就是文件路径本身，不能拼 `::`。
        2. 类内测试 —— classname="tests.test_foo.TestBar"。pytest 要的是
           `tests/test_foo.py::TestBar::test_baz`，classname 尾部超出模块
           路径的那些段就是类名链（支持嵌套类）。
        3. 模块级测试 —— classname="tests.test_foo"，直接 `文件::用例`。

        无效 id 的代价不是报错而是静默：它进了 pytest 命令行，pytest 在收集
        阶段整轮中止（exit 4），一个用例都不跑，写出一份 tests="0" 的空报告 ——
        报告存在，require_report 查不出异常，看起来像「全部复跑通过」。
        """
        if not classname:
            return file or name
        if file:
            stem = PurePosixPath(file).with_suffix("").parts
            cls = classname.split(".")
            # classname 前缀与文件路径对不上时（rootdir 不同等），退回不带
            # 类名的形式 —— 与本函数改造前的行为一致，不会更糟
            classes = list(cls[len(stem):]) if list(cls[:len(stem)]) == list(stem) else []
            return "::".join([file, *classes, name])
        # file 缺失（别的适配器 / xunit1 哪天消失）：从尾部剥掉首字母大写的段。
        # pytest 默认 python_classes = Test*，类名必然大写开头；模块名按
        # PEP 8 小写。第一段永不剥 —— 全大写的退化输入不能把路径剥空。
        parts = classname.split(".")
        i = len(parts)
        while i > 1 and parts[i - 1][:1].isupper():
            i -= 1
        path = "/".join(parts[:i]) + ".py"
        return "::".join([path, *parts[i:], name])
```

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_pytest_adapter.py tests/test_junit.py -q` → 全绿。

- [ ] **步骤 5：跑受影响的既有测试**

`uv run pytest tests/test_junit.py tests/test_tool_runtests.py tests/test_flaky.py tests/test_nodes_preflight_baseline.py -q` → 全绿。
若有测试因 id 形状变化而红，**先判断是测试写死了旧的错误形状还是真的回归**，在报告里写明。

- [ ] **步骤 6：Commit**

```bash
git -c user.name=sumengnan -c user.email=2499165351@qq.com add src/aifix/adapters/pytest_adapter.py tests/test_pytest_adapter.py
git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "fix(adapter): 类内测试与收集错误的 test_id 现在可重跑"
```

---

### 任务 2：`split_paths` 收测试目录下的非 `.py` 与 `conftest.py`

**背景**：非 `.py` 路径直接被丢弃。某个 commit 同时新增了测试所需的夹具（数据文件、快照、配置片段）时，该文件不会被 `materialize` 嫁接，任务在 base 侧因缺文件而红、在 C 侧绿，通过全部现有校验进入任务集，但 ground truth 实际不可达。另外根目录的 `conftest.py`（不在 `test_dirs` 里、不以 `test_` 开头）当前被判成源文件进 `gold_files`——它是测试基础设施，不是 ground truth。

**文件：**
- 修改：`src/aifix/eval/mine.py`
- 测试：`tests/test_eval_mine.py`

- [ ] **步骤 1：写失败测试**

```python
def test_fixture_files_under_test_dirs_go_with_the_tests():
    """测试目录下的非 .py 夹具必须跟着测试一起嫁接，否则 ground truth 不可达。"""
    tests, src = split_paths(
        ["tests/test_a.py", "tests/data/golden.json", "tests/fixtures/x.sql",
         "src/pkg/mod.py", "README.md", "assets/logo.png"],
        ["tests", "test"])
    assert tests == ["tests/test_a.py", "tests/data/golden.json",
                     "tests/fixtures/x.sql"]
    # 非测试目录的非 .py 不进 gold_files：gold 是 locate_hit 的判定依据，
    # 衡量的是定位**源文件**的能力，塞进数据文件会稀释这个指标
    assert src == ["src/pkg/mod.py"]


def test_conftest_is_test_infrastructure_not_ground_truth():
    """根目录 conftest.py 既不在 test_dirs 里也不以 test_ 开头。"""
    tests, src = split_paths(["conftest.py", "src/pkg/mod.py"], ["tests"])
    assert tests == ["conftest.py"]
    assert src == ["src/pkg/mod.py"]
```

- [ ] **步骤 2：跑测试确认失败**

`uv run pytest tests/test_eval_mine.py -q -k "fixture or conftest"`
预期：第一条失败（`tests` 里只有 `tests/test_a.py`）；第二条失败（`conftest.py` 落进 `src`）。

- [ ] **步骤 3：实现**

```python
def split_paths(paths: list[str],
                test_dirs: list[str]) -> tuple[list[str], list[str]]:
    """把 commit 改动的路径拆成（测试侧, 源文件）。

    「测试侧」不只是 .py：测试目录下的夹具（数据文件、快照、配置片段）必须
    跟着测试一起被 materialize 嫁接。少了它们，任务在 base 侧因缺文件而红、
    在 C 侧绿，通过全部现有校验进入任务集，但 ground truth 实际不可达 ——
    修复模型即便诊断和补丁都对也过不了。这不是捏造任务（确实是红转绿），
    是任务质量问题。

    非测试目录下的非 .py 一律忽略，**不进 gold_files**：gold 是 locate_hit
    的判定依据，衡量的是 Detector 定位**源文件**的能力，塞进数据文件会稀释
    这个指标。
    """
    tests: list[str] = []
    src: list[str] = []
    for p in paths:
        pp = PurePosixPath(p)
        in_test_dir = bool(pp.parts) and pp.parts[0] in test_dirs
        # conftest.py 可能躺在仓库根目录 —— 既不在 test_dirs 里，也不以
        # test_ 开头，会被判成源文件进 gold_files。它是测试基础设施。
        if in_test_dir or pp.name == "conftest.py" or pp.name.startswith("test_"):
            tests.append(p)
        elif pp.suffix == ".py":
            src.append(p)
    return tests, src
```

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_eval_mine.py -q` → 全绿。

- [ ] **步骤 5：Commit**

```bash
git -c user.name=sumengnan -c user.email=2499165351@qq.com add src/aifix/eval/mine.py tests/test_eval_mine.py
git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "fix(mine): 测试夹具与 conftest 归入测试侧"
```

---

## 阶段二 · 挖掘提速与召回（D 组）

### 任务 3：`verify_commit` 改成两次 scoped + 一次全量确认

**背景**：现在对每个候选 commit 跑**两次全量**。实测本仓库全量 171 秒，一个候选 commit 约 6 分钟，98 个提交里 65 个是候选。用一段一次性探针改成 scoped 后，8 个候选 commit 产出 15 个可用用例只花约 13 分钟（同样这 8 个走两次全量约 48 分钟）。

**新流程**：

| 阶段 | 范围 | 状态 | 作用 |
|---|---|---|---|
| 1 | scoped 到 `test_files` | C^ 源码 + C 测试 | `red` |
| 2 | scoped 到 `test_files` | C | `green` |
| 3 | scoped 到 `cand` | C | 复跑确认（现有逻辑） |
| 4 | **全量** | 回到阶段 1 的状态 | 确认候选在全量下也红 |

阶段 4 不能省：评测时 `run_task` 用**全量** baseline 复现 `target_test`。scoped 下红、全量下绿的用例会在评测时变成 `error`，安全但浪费。把这个浪费挪到挖掘时一次性付清。

阶段 3 仍要保留：它比 1/2 更窄（只跑候选本身），排的是"在测试文件这个范围内碰巧绿了"；阶段 4 排的是"在全量范围内碰巧红了"。两个方向。

**文件：**
- 修改：`src/aifix/eval/mine.py`
- 测试：`tests/test_eval_mine.py`

- [ ] **步骤 1：写失败测试**

需要一个真的 git 仓库夹具。仓库里已有构造仓库的辅助（看 `tests/test_eval_mine.py` 与 `tests/test_eval_workspace.py` 现有写法，**复用它们，不要另造一套**）。

```python
async def test_scoped_stage_does_not_run_the_whole_suite(tmp_path, monkeypatch):
    """阶段 1/2 只跑 test_files —— 断言传给 pytest 的参数里确实只有它们。"""
    # 用 monkeypatch 记录 run_scoped / run_full_suite 的调用次数与参数，
    # 断言：run_scoped 被调用 3 次（阶段 1、2、3），run_full_suite 1 次（阶段 4），
    # 且阶段 1/2 的 test_ids 恰好等于 test_files。


async def test_candidate_red_only_under_scope_is_dropped(tmp_path):
    """造一个「scoped 下红、全量下绿」的仓库，断言候选被阶段 4 排除。

    做法：测试文件 A 单跑时红（依赖一个全局状态没被初始化），但全量跑时
    另一个测试文件先跑并初始化了那个全局状态，于是它绿。
    """
```

第二条是本任务的核心断言——它是阶段 4 存在的唯一理由，必须真的能红。

- [ ] **步骤 2：跑测试确认失败**

`uv run pytest tests/test_eval_mine.py -q -k "scoped or only_under_scope"`
预期：第一条失败（现在 `run_full_suite` 被调 2 次、`run_scoped` 1 次）；第二条失败（候选没被排除）。

- [ ] **步骤 3：实现**

`verify_commit` 重写（保留现有 docstring 里仍然成立的部分，删掉已被任务 1 修掉的那段关于 `make_test_id` 的描述）：

```python
async def verify_commit(repo: str, commit: str, base_commit: str,
                        test_files: list[str], adapter: PytestAdapter,
                        workdir: Path) -> list[str]:
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    materialize(repo, base_commit, commit, test_files, workdir)
    # materialize 之后的 HEAD 就是「C^ 源码 + C 测试」这个状态。它可能是
    # base_commit 本身（测试无差异时不建提交），也可能是 materialize 新建的
    # 那个提交 —— 阶段 4 要回到这里，所以现在就记下来
    staged_head = _git(workdir, "rev-parse", "HEAD").strip()

    scope = [p for p in test_files if PurePosixPath(p).suffix == ".py"]
    if not scope:
        return []
    red = await run_scoped(workdir, adapter, scope, require_report=True)
    if not red.ids:
        return []
    _git(workdir, "checkout", "--force", "--quiet", commit)
    green = await run_scoped(workdir, adapter, scope, require_report=True)

    cand = (red.ids - green.ids) & green.ran
    # 文件级 id（收集错误）：green 侧该文件正常收集，发出的是各个用例，
    # 文件级 id 本身不会出现在 green.ran 里 —— 光靠上面那行会把「测试文件
    # 在 C^ 导入失败、在 C 正常」整类候选静默丢掉。实测本仓库 65 个候选
    # commit 里 32 个新增了测试文件，那正是这一类。
    cand |= {i for i in (red.ids - green.ids)
             if "::" not in i and _file_went_green(i, green)}
    if not cand:
        return []
    recheck = await run_scoped(workdir, adapter, sorted(cand),
                               require_report=True)
    cand = {i for i in cand
            if (_file_went_green(i, recheck) if "::" not in i
                else (i in recheck.ran and i not in recheck.ids))}
    if not cand:
        return []
    # 阶段 4：回到 C^ 状态跑一次全量。评测时 run_task 是用全量 baseline
    # 复现 target_test 的，scoped 下红、全量下绿的用例（顺序依赖、状态
    # 污染）到那时会变成 error —— 安全，但白跑一次模型。把这份浪费挪到
    # 这里一次性付清
    _git(workdir, "checkout", "--force", "--quiet", staged_head)
    red_full = await run_full_suite(workdir, adapter, require_report=True)
    return sorted(cand & red_full.ids)


def _file_went_green(file_id: str, fs) -> bool:
    """文件级 id 在 fs 这一侧「该文件的用例至少跑到一个且全部通过」。

    要求「至少跑到一个」而不只是「没有失败」：文件根本没被收集时同样
    没有失败，那不是变绿。
    """
    cases = {i for i in fs.ran if i.startswith(file_id + "::")}
    return bool(cases) and not (cases & fs.ids)
```

同时 `mine_tasks` 里 `verify_commit` 的调用不变。

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_eval_mine.py -q` → 全绿。

- [ ] **步骤 5：Commit**

```bash
git -c user.name=sumengnan -c user.email=2499165351@qq.com add src/aifix/eval/mine.py tests/test_eval_mine.py
git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "perf(mine): 两次 scoped + 一次全量确认，并收回收集错误那一类候选"
```

---

### 任务 4：挖掘真跑一轮，验证任务集非空且可用

**这是一个验证任务，不是实现任务。**

- [ ] **步骤 1：真跑挖掘**

```bash
uv run aifix mine . --limit 40 --max-tasks 12 --out /tmp/m4-tasks.jsonl
```

- [ ] **步骤 2：核对产出**

- 任务数 ≥ 8（探针数据支持这个量级：8 个候选 commit 出 15 个用例）
- 至少有一个 `target_test` **不含 `::`**（文件级 id，来自收集错误那一类）——若一个都没有，说明任务 3 的那段代码没有被真实数据走到，在报告里写明
- 每条 `gold_files` 非空且都是 `.py`

- [ ] **步骤 3：抽验一条任务确实红**

取任务集第一条，`prepare_task_repo` 到临时目录，跑全量，断言 `target_test` 在失败集里。写一段一次性脚本，不入库。

- [ ] **步骤 4：把耗时与任务数写进报告文件**（不 commit 产物）

---

## 阶段三 · 统计诚实性（D 组）

### 任务 5：Wilson score interval

**背景**：`1/1 = 100%` 与 `12/20 = 60%` 现在渲染成同一种东西。选 Wilson 而不是正态近似（Wald）：`p̂=1, n=1` 时 Wald 给出 `[100%, 100%]`——一个宣称"确定无疑"的区间，正是本里程碑要消灭的东西。

**文件：**
- 创建：`src/aifix/eval/stats.py`
- 测试：`tests/test_eval_stats.py`

- [ ] **步骤 1：写失败测试**

```python
from aifix.eval.stats import wilson


def test_single_perfect_sample_is_not_a_conclusion():
    """1/1 = 100% 的区间下界必须远离 100% —— 这是整个模块存在的理由。"""
    lo, hi = wilson(1, 1)
    assert 0.20 < lo < 0.22, lo        # 精确值 0.2065
    assert hi == 1.0


def test_larger_sample_narrows_the_interval():
    lo1, hi1 = wilson(6, 10)
    lo2, hi2 = wilson(60, 100)
    assert (hi1 - lo1) > (hi2 - lo2) * 2.5
    # 都以 0.6 为中心附近
    assert lo1 < 0.6 < hi1 and lo2 < 0.6 < hi2


def test_known_values():
    """对着教科书数值断言，不是断言「区间存在」。"""
    lo, hi = wilson(0, 10)
    assert lo == 0.0
    assert 0.27 < hi < 0.29            # 精确值 0.2775
    lo, hi = wilson(5, 10)
    assert 0.23 < lo < 0.25            # 0.2366
    assert 0.75 < hi < 0.77            # 0.7634


def test_zero_sample():
    assert wilson(0, 0) == (0.0, 0.0)
```

- [ ] **步骤 2：跑测试确认失败**（模块不存在）

- [ ] **步骤 3：实现**

```python
"""区间估计。样本量少的时候，一个百分比不是结论。"""
from __future__ import annotations

# 95% 双侧
Z95 = 1.959963984540054


def wilson(hits: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval。返回 (下界, 上界)，都在 [0, 1]。

    为什么不用正态近似（Wald，p̂ ± z·√(p̂(1-p̂)/n)）：p̂ 取 0 或 1 时方差算出
    0，区间塌成一个点 —— 1/1 会得到 [100%, 100%]，一个宣称"确定无疑"的
    区间。而"只跑了一个任务"恰恰是这张对比表最需要说出口的事。Wilson 在
    同样输入下给出 [21%, 100%]，一眼看出没有结论。

    n = 0 时返回 (0, 0)：没有样本就没有区间，调用方据此渲染成「—」。
    """
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(center - half, 0.0), min(center + half, 1.0))
```

- [ ] **步骤 4：跑测试确认通过** → 全绿。

- [ ] **步骤 5：Commit**

```bash
git -c user.name=sumengnan -c user.email=2499165351@qq.com add src/aifix/eval/stats.py tests/test_eval_stats.py
git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "feat(eval): Wilson 区间 —— 样本量不足时让表自己说出来"
```

---

### 任务 6：对比表渲染分数与区间

**文件：**
- 修改：`src/aifix/eval/score.py`
- 测试：`tests/test_eval_score.py`

- [ ] **步骤 1：写失败测试**

```python
def test_table_shows_fraction_and_interval():
    """1/1 的 100% 必须在表格里就能看出「只有一个样本」。"""
    r = TaskResult(task_id="t", model="m", locate_hit=True, suspect_file="a.py",
                   verdict="better", attempts=1, tokens=10, cost_usd=0.1,
                   violations=0)
    table = render_table([summarize([r])])
    assert "100% (1/1" in table
    assert "21%" in table          # Wilson 下界，见 stats.wilson(1,1)
    # 反向钉死：不能只显示一个光秃秃的 100%
    assert "| 100% |" not in table


def test_zero_valid_tasks_renders_dash_not_zero_percent():
    """全是评测故障时，比率没有意义，不能显示 0%（会被读成「一个都没修好」）。"""
    r = TaskResult(task_id="t", model="m", locate_hit=False, suspect_file=None,
                   verdict="same", attempts=0, tokens=0, cost_usd=0.0,
                   violations=0, error="克隆失败")
    table = render_table([summarize([r])])
    assert "0%" not in table
    assert "—" in table
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

`Summary` 增加两个计数字段（表格要渲染分数，必须知道分子）：

```python
class Summary(BaseModel):
    model: str
    tasks: int
    locate_hits: int            # 分子。光有比率渲染不出 (12/20)
    fix_hits: int
    locate_rate: float
    fix_rate: float
    ...
```

`summarize` 里填上；`n == 0` 分支同样填 0。

```python
def _rate_cell(hits: int, n: int) -> str:
    """比率、分数、95% 区间一起给。

    只给比率会让 1/1 的 100% 和 12/20 的 60% 长得一样重 —— M3 那张只有一个
    样本的对比表就是这么被读成结论的。n = 0 时不渲染任何数字：0% 会被读成
    「一个都没修好」，而真相是「一个有效任务都没有」。
    """
    if n <= 0:
        return "—"
    lo, hi = wilson(hits, n)
    return f"{hits / n:.0%} ({hits}/{n}, 95%CI {lo:.0%}–{hi:.0%})"
```

`render_table` 的对应两列改用 `_rate_cell`。

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_eval_score.py -q` → 全绿。

- [ ] **步骤 5：Commit**

---

## 阶段四 · 来源与人造变异（D 组）

### 任务 7：`Task` / `TaskResult` 加 `origin` 与 `mutation_diff`，`materialize` 施加变异

**文件：**
- 修改：`src/aifix/eval/task.py`、`src/aifix/eval/workspace.py`
- 测试：`tests/test_eval_task.py`、`tests/test_eval_workspace.py`

- [ ] **步骤 1：写失败测试**

```python
def test_materialize_applies_the_mutation_and_leaves_a_clean_tree(tmp_path):
    """变异补丁必须被打上并提交 —— preflight 会拒绝不干净的仓库。"""
    # 造一个 git 仓库，HEAD 有 mod.py: def f(): return 1
    # mutation_diff 把 return 1 改成 return 2
    dest = tmp_path / "dest"
    materialize(str(repo), head, head, [], dest, mutation_diff=diff)
    assert (dest / "mod.py").read_text().strip().endswith("return 2")
    out = subprocess.run(["git", "status", "--porcelain"], cwd=dest,
                         capture_output=True, text=True).stdout
    assert out.strip() == "", f"工作区不干净：{out}"


def test_task_defaults_keep_old_jsonl_readable():
    """老任务集文件没有这两个字段，必须仍能读进来。"""
    line = ('{"task_id":"t","repo":"/r","commit":"a","base_commit":"b",'
            '"test_files":[],"target_test":"x","gold_files":["s.py"]}')
    t = Task.model_validate_json(line)
    assert t.origin == "mined" and t.mutation_diff is None
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

`task.py`：

```python
class Task(BaseModel):
    ...
    adapter: str = "pytest"
    # C 类（人造变异）任务：施加在 base_commit 之上的 unified diff。
    # 不在源仓库里建提交 —— 那会污染用户的仓库；补丁随任务集走，
    # 任务集是一份自包含的 jsonl
    mutation_diff: str | None = None
    # mined（从 git history 挖）| mutated（人造变异）。两者分布不同，
    # 成功率不能平均成一个数字，见 score.summarize_by_origin
    origin: str = "mined"


class TaskResult(BaseModel):
    ...
    origin: str = "mined"
    # 补丁合理性静态信号的条数（见 aifix.signals）。不改判定，只标注
    signals: int = 0
```

`workspace.py`：

```python
def materialize(repo: str, base_commit: str, commit: str,
                test_files: list[str], dest: Path,
                mutation_diff: str | None = None) -> Path:
    ...
    if test_files:
        _git(dest, "checkout", commit, "--", *test_files)
    if mutation_diff:
        # --unsafe-paths 不加：补丁只应落在仓库内。git apply 失败要抛，
        # 不能静默跳过 —— 没打上变异的任务是绿的，会被评测当成
        # 「baseline 未复现」，浪费一次克隆和一次全量
        proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                              cwd=dest, input=mutation_diff,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"变异补丁打不上（{dest}）：{proc.stderr.strip()}")
    _git(dest, "config", ...)
    # add 的范围要覆盖测试文件与变异改动的文件
    ...
```

注意 `git add` 现在要覆盖变异改动的文件。最简单且安全的做法：变异存在时 `_git(dest, "add", "--all")`——**这里可以用 `--all`**，因为 dest 是刚克隆出来、还没跑过任何测试的干净目录，不存在构建产物（与 `Worktree.commit` 的情形不同，那里禁止 `add -A` 是因为 worktree 里跑过测试）。把这个理由写进注释。

`prepare_task_repo` 透传 `task.mutation_diff`。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 8：AST 变异算子

**文件：**
- 创建：`src/aifix/eval/mutate.py`（本任务只做算子部分）
- 测试：`tests/test_eval_mutate.py`

**定位（写进模块 docstring）**：这是**冒烟集**，不是基准。变异的分布与真实 bug 不同——它便宜、确定、可任意规模，用来验证链路本身是否工作；拿它跨模型比高低是过度解读。

- [ ] **步骤 1：写失败测试**

```python
def test_operators_produce_single_point_changes():
    """每个变异只动一处 —— 多处变异让 ground truth 不再是单点。"""
    src = "def f(a, b):\n    if a < b:\n        return a + 1\n    return True\n"
    muts = list(mutations(src))
    assert len(muts) >= 4
    for m in muts:
        # 与原文恰好差一处：逐行比对只有一行不同
        diff_lines = [(x, y) for x, y in zip(src.splitlines(), m.source.splitlines())
                      if x != y]
        assert len(diff_lines) == 1, (m.description, diff_lines)


def test_comparison_flip():
    src = "def f(a, b):\n    return a < b\n"
    got = {m.source for m in mutations(src)}
    assert "def f(a, b):\n    return a <= b\n" in got


def test_mutated_source_still_parses():
    src = (Path("src/aifix/eval/score.py")).read_text(encoding="utf-8")
    for m in mutations(src):
        ast.parse(m.source)      # 语法坏掉的变异是废品，不是任务
```

- [ ] **步骤 2：跑测试确认失败**（模块不存在）

- [ ] **步骤 3：实现**

```python
@dataclass(frozen=True)
class Mutation:
    source: str          # 变异后的完整文件内容
    lineno: int
    description: str     # 形如 "比较运算符 < → <="


def mutations(source: str) -> Iterator[Mutation]:
    """对一份源码逐点施加变异，每次只动一处。

    用 ast.unparse 重新生成整个文件会把格式、注释全部抹掉 —— 那样产出的
    diff 是「整文件重写」，既不像真实 bug，也会当场撞上巨型 diff 守卫。
    所以走**文本替换**：用 AST 只负责定位（拿到 col_offset），替换在原文
    的那一行上做。
    """
```

算子表（`ast.Compare` / `ast.BinOp` / `ast.BoolOp` / `ast.Constant`）：

| 节点 | 变换 |
|---|---|
| `ast.Lt`↔`ast.LtE`，`ast.Gt`↔`ast.GtE`，`ast.Eq`↔`ast.NotEq` | `<`↔`<=`，`>`↔`>=`，`==`↔`!=` |
| `ast.Add`↔`ast.Sub`，`ast.Mult`↔`ast.FloorDiv` | `+`↔`-`，`*`↔`//` |
| `ast.And`↔`ast.Or` | `and`↔`or` |
| `Constant(True)`↔`Constant(False)` | |
| `Constant(int)` | `n` → `n+1` |

实现要点：
- 定位靠 AST（`node.lineno` / `node.col_offset` / `end_col_offset`），替换靠字符串切片，其余字节原样保留
- 运算符节点在 Python 3.8+ **没有** `col_offset`。做法：拿到左右操作数的 `end_col_offset` 与 `col_offset`，在两者之间的那一段里找运算符文本并替换。这一段可能跨行——跨行的跳过，不值得为它复杂化
- 产出后 `ast.parse` 自检，parse 不过就丢弃（不 yield）
- 顺序稳定（按 lineno, col 排序），保证可复现

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 9：变异验证与任务产出

**文件：**
- 修改：`src/aifix/eval/mutate.py`
- 测试：`tests/test_eval_mutate.py`

- [ ] **步骤 1：写失败测试**

```python
async def test_generated_task_is_actually_red(tmp_path):
    """产出的任务必须**真的红** —— 断言「生成了 N 条记录」证明不了任何事。"""
    repo = _make_green_repo(tmp_path)     # HEAD 全绿的小仓库
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=2)
    assert tasks, "一个任务都没产出"
    for t in tasks:
        assert t.origin == "mutated" and t.mutation_diff
        dest = tmp_path / "check" / t.task_id[-8:]
        prepare_task_repo(t, dest)
        fs = await run_full_suite(dest, PytestAdapter(), require_report=True)
        assert t.target_test in fs.ids, \
            f"任务不红：{t.target_test} 不在 {sorted(fs.ids)}"


async def test_refuses_a_repo_that_is_already_red(tmp_path):
    """本来就红的仓库上做变异，分不清红是变异造成的还是本来就有的。"""
    repo = _make_red_repo(tmp_path)
    with pytest.raises(RuntimeError, match="不是全绿"):
        await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1)


async def test_drops_mutations_that_break_too_much(tmp_path):
    """把套件炸掉一半的变异不是好任务 —— 它太显眼，且违反单点缺陷前提。"""
    # max_new_failures=1 时，一个会让 3 个用例同时红的变异必须被丢弃
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

```python
async def mutate_tasks(repo: str, adapter, max_tasks: int = 10,
                       max_new_failures: int = 5,
                       scope: str = "smart",
                       seed: int = 0,
                       workdir: Path | None = None,
                       on_progress=None) -> list[Task]:
    """在一个全绿仓库上生成人造变异任务。"""
```

流程：
1. 克隆 repo 到 workdir（用 `materialize(repo, HEAD, HEAD, [], workdir)`，复用现成的）
2. 跑一次全量，`require_report=True`。**有失败即抛** `RuntimeError("仓库 HEAD 不是全绿，无法做变异……")`
3. 从这次全量的报告里建 `文件 → 用例` 索引（xunit1 的 `file` 属性，任务 1 的红利）
4. 枚举源文件：`git ls-files '*.py'` 减去测试侧（复用 `split_paths`）
5. 对每个源文件的每个变异候选（`seed` 控制打乱顺序，保证可复现）：
   - 写入工作副本 → 跑测试（`smart`：只跑词干相关的测试文件；`full`：全量）→ 还原文件
   - 新失败数在 `[1, max_new_failures]` 之内才收
   - `git diff -- <文件>` 取 unified diff 作为 `mutation_diff`
   - 产出 `Task(task_id=f"{name}@mut::{相对路径}:{行号}::{target}", repo=..., commit=HEAD, base_commit=HEAD, test_files=[], target_test=新失败里的一个, gold_files=[被变异文件], origin="mutated", mutation_diff=diff)`
6. 够 `max_tasks` 就停；结束清理 workdir

`smart` 范围的词干匹配：源文件 `src/aifix/eval/mine.py` 的 stem 是 `mine`，候选测试文件是**文件名包含该 stem** 的那些。匹配不到就跳过这个文件（**漏是安全的**：不产生假任务，只是少一个候选）。这一条要写进注释与 `--help`。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 10：`aifix mutate` 子命令

**文件：**
- 修改：`src/aifix/cli.py`
- 测试：`tests/test_cli_args.py`

- [ ] **步骤 1：写失败测试**

```python
def test_mutate_subcommand_exists_and_states_its_positioning():
    """帮助文本必须说清这是冒烟集不是基准 —— 否则这些数字会被当成结论。"""
    text = _strip(_sub_help("mutate"))     # 剥 ANSI + 删全部空白，见既有 helper
    assert _strip("冒烟集") in text
    assert _strip("不是基准") in text


def test_mutate_flags():
    args = build_parser().parse_args(
        ["mutate", ".", "--max-tasks", "3", "--scope", "full"])
    assert args.max_tasks == 3 and args.scope == "full"
```

**注意**：`_sub_help` 的断言必须走既有的「剥 ANSI + 删全部空白」归一化。中文帮助文本没有词间空格，`textwrap` 在任意位置硬断，任何保留空白的比对都会随终端宽度飘（M3b 里 `COLUMNS=45` 当场红过）。若 `tests/test_cli_args.py` 里已有这个 helper，**复用它**。

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

```python
    mut = sub.add_parser("mutate", help="人造变异生成冒烟任务集")
    mut.add_argument("repo", nargs="?", default=".")
    mut.add_argument("--max-tasks", type=int, default=10)
    mut.add_argument("--max-new-failures", type=int, default=5,
                     help="一个变异最多允许弄红几个用例。超过即丢弃 ——"
                          "把套件炸掉一半的变异太显眼，也违反单点缺陷前提")
    mut.add_argument("--scope", choices=["smart", "full"], default="smart",
                     help="smart 只跑与被变异文件词干相关的测试文件（快，会漏，"
                          "漏了只是少一个候选，不产生假任务）；full 每个变异跑一次全量")
    mut.add_argument("--seed", type=int, default=0)
    mut.add_argument("--out", default="evals/tasks-mutants.jsonl")
```

`mut` 的 `description` 写定位：「产出的是**冒烟集**，不是基准。变异的分布与真实 bug 不同——它便宜、确定、可任意规模，用来验证链路本身是否工作；拿它跨模型比高低是过度解读。」

`_cmd_mutate` 照 `_cmd_mine` 的形状写（延迟导入、进度回调、`write_jsonl`）。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 11：来源不混算

**文件：**
- 修改：`src/aifix/eval/score.py`、`src/aifix/eval/runner.py`、`src/aifix/cli.py`
- 测试：`tests/test_eval_score.py`

- [ ] **步骤 1：写失败测试**

```python
def test_mixed_origins_are_not_averaged_into_one_number():
    """挖掘与变异分布不同，成功率平均成一个数字是错的。"""
    rs = [_result(origin="mined", verdict="better"),
          _result(origin="mined", verdict="same"),
          _result(origin="mutated", verdict="better"),
          _result(origin="mutated", verdict="better")]
    summaries = summarize_by_origin(rs)
    assert [s.origin for s in summaries] == ["mined", "mutated"]
    assert summaries[0].fix_hits == 1 and summaries[0].tasks == 2
    assert summaries[1].fix_hits == 2 and summaries[1].tasks == 2


def test_single_origin_stays_one_row():
    rs = [_result(origin="mined", verdict="better")]
    assert len(summarize_by_origin(rs)) == 1
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

- `Summary` 加 `origin: str = ""`
- `summarize(results, origin="")` 透传
- `summarize_by_origin(results)`：按 `r.origin` 分组（顺序按首次出现），单一来源时返回 `[summarize(results, origin=那个来源)]`
- `render_table` 加「来源」列，`origin` 为空时渲染 `—`
- `runner.run_task` 把 `task.origin` 填进 `TaskResult`；`_blank` 也要接受并填 `origin`（否则被跳过的变异任务会被归到 `mined`）
- `cli._cmd_eval` / `_cmd_eval_report` 改用 `summarize_by_origin`

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_eval_score.py tests/test_eval_runner.py tests/test_cli_args.py -q` → 全绿。

- [ ] **步骤 5：Commit**

---

## 阶段五 · 补丁合理性静态信号（A 组）

### 任务 12：`signals.py`

**背景（真实验收实证）**：探针任务的断言是 `add(1,1)==2 and add(1,1)==3`（逻辑上不可能）。模型把 `add` 改成有状态函数满足它、顺手删掉了无测试覆盖的 `mul`，系统报告「修复 1/1」并给出 merge 命令。每一道守卫都正常工作了——它们检查的都是 agent 的**行为**（改没改测试、diff 大不大、越没越界），没有一道检查补丁的**合理性**。

**边界**：只标注，**不改判定**。三态判定仍然只看测试结果。规格 §1 的「审查者是人」不变——本任务做的是让那个人有东西可看。

**文件：**
- 创建：`src/aifix/signals.py`
- 测试：`tests/test_signals.py`

- [ ] **步骤 1：写失败测试**

```python
_OLD = '''
def add(a, b):
    return a + b

def mul(a, b):
    return a * b

class Calc:
    def total(self, xs):
        return sum(xs)
    def _helper(self):
        pass
'''

_NEW = '''
_CALLS = {}

def add(a, b):
    _CALLS[(a, b)] = _CALLS.get((a, b), 0) + 1
    return a + b + (1 if _CALLS[(a, b)] > 1 else 0)

class Calc:
    def total(self, xs):
        return sum(xs)
'''


def test_catches_the_real_specification_gaming_case():
    """M3 真实验收那一幕：删掉无测试覆盖的公开函数 + 新增模块级可变状态。"""
    s = analyze({"calc.py": (_OLD, _NEW)}, suspect="calc.py")
    assert "mul" in s.removed_public_symbols
    assert "Calc._helper" not in s.removed_public_symbols   # 私有的不报
    assert "_CALLS" in s.new_module_state
    assert s.files_outside_suspect == []
    assert s.count == 2


def test_clean_patch_produces_no_signal():
    """正常的修复不该报任何信号 —— 否则这一列会被无视。"""
    old = "def add(a, b):\n    return a - b\n"
    new = "def add(a, b):\n    return a + b\n"
    s = analyze({"calc.py": (old, new)}, suspect="calc.py")
    assert s.count == 0 and s.is_empty()


def test_edits_outside_the_suspect_file():
    s = analyze({"calc.py": ("x = 1\n", "x = 2\n"),
                 "other.py": ("y = 1\n", "y = 2\n")}, suspect="calc.py")
    assert s.files_outside_suspect == ["other.py"]


def test_new_file_is_not_a_removal():
    s = analyze({"new.py": (None, "def f(): pass\n")}, suspect=None)
    assert s.removed_public_symbols == []


def test_syntax_error_does_not_raise():
    """补丁把文件写坏了 —— 测试自然会红，信号模块不该跟着崩。"""
    s = analyze({"a.py": ("def f(): pass\n", "def f( :\n")}, suspect=None)
    assert s.count >= 0     # 只要不抛
```

- [ ] **步骤 2：跑测试确认失败**（模块不存在）

- [ ] **步骤 3：实现**

```python
"""补丁合理性的静态信号。零模型调用，纯 AST。

不改判定 —— 三态判定仍然只看测试结果。这里做的是给**人**一个信号：
规格 §1 定的是「审查者是人」，但 M3 真实验收里报告没有给人任何值得多看
一眼的东西。模型把 add 改成有状态函数去满足一个自相矛盾的断言、顺手删掉
无测试覆盖的 mul，每一道守卫都正常工作了：它们检查的都是 agent 的**行为**
（改没改测试、diff 大不大、越没越界），没有一道检查补丁的**合理性**。

明确的局限：静态信号挡不住「在测试覆盖范围内把实现改成特例硬编码」。那
需要覆盖率差分甚至语义分析。这不是一个能靠加信号彻底解决的问题 —— 它是
测试覆盖率作为天花板的直接后果。
"""

@dataclass(frozen=True)
class PatchSignals:
    removed_public_symbols: list[str]
    new_module_state: list[str]
    files_outside_suspect: list[str]

    @property
    def count(self) -> int:
        return (len(self.removed_public_symbols) + len(self.new_module_state)
                + len(self.files_outside_suspect))

    def is_empty(self) -> bool:
        return self.count == 0


def public_symbols(source: str) -> set[str]:
    """模块级 def/class，以及类里的方法（表示为 Class.method）。名字不以 _ 开头。

    刻意不看变量：__all__ 之外的模块级常量改名太常见，会把信号淹掉。
    """


def module_state(source: str) -> set[str]:
    """模块级的可变赋值名：右值是 list/dict/set 字面量或推导式，
    或对 list()/dict()/set() 的调用。

    模块级可变状态是「把纯函数改成有状态函数」最直接的指纹。
    """


def analyze(files: dict[str, tuple[str | None, str | None]],
            suspect: str | None) -> PatchSignals:
    """files: 路径 → (旧内容, 新内容)。None 表示该侧不存在（新增 / 删除）。"""
```

`ast.parse` 抛 `SyntaxError` 时该侧当作空集合——补丁把文件写坏了，测试自然会红，信号模块不该跟着崩。

**只对 `.py` 做 AST 分析**（在 `analyze` 内部按后缀过滤，不要求调用方过滤）。`files_outside_suspect` 则对**所有**被改动的文件都算——改了一个 `.json` 配置也是"落在嫌疑文件之外"，那正是这个信号要说的事。

`files_outside_suspect`：`suspect` 为 `None` 时返回 `[]`（没有诊断就没有"之外"）。比较用 `eval/runner.py` 里 `locate_hit` 那套**路径分段后缀匹配**——模型给的 `suspect_file` 可能是模块路径形式（`aifix/eval/mine.py`）而 `touched` 是仓库路径形式（`src/aifix/eval/mine.py`）。**把 `_path_parts` 和后缀匹配从 `eval/runner.py` 提到 `signals.py` 或一个共用位置，两处引用同一份实现**，不要复制一份——复制出来的两份会各自漂移。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 13：信号接入 verify → 报告

**文件：**
- 修改：`src/aifix/delivery.py`、`src/aifix/nodes/verify.py`、`src/aifix/nodes/report.py`、`src/aifix/graph.py`
- 测试：`tests/test_delivery.py`、`tests/test_e2e.py` 或新建 `tests/test_signals_wiring.py`

- [ ] **步骤 1：写失败测试**

```python
def test_file_at_head_reads_the_committed_version(tmp_path):
    """改动还没提交时，旧内容只能从 HEAD 拿。"""
    # 建仓库 + worktree，改文件不提交，断言 file_at_head 返回旧内容
    # 且对不存在于 HEAD 的新文件返回 None


async def test_verify_records_signals_and_report_shows_them(...):
    """删掉一个公开函数 + 新增模块级 dict，报告必须有「值得多看一眼」。"""
    # 用 scripted client 驱动一次 run，让模型做出那种补丁
    assert "值得多看一眼" in state["report_md"]
    assert "mul" in state["report_md"]
    # 判定不受影响 —— 信号只标注
    assert state["results"][0]["verdict"] == "better"


def test_clean_patch_report_has_no_signal_section(...):
    """正常修复的报告里不该出现这一节，否则它会被无视。"""
    assert "值得多看一眼" not in state["report_md"]
```

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

`delivery.py`：

```python
    def file_at_head(self, path: str) -> str | None:
        """worktree 里 HEAD 版本的文件内容；HEAD 里没有这个文件时返回 None。

        信号计算要在 commit 之前做 —— 那时改动还在工作区，旧内容只能
        从 HEAD 拿。
        """
        res = _git(self.path, "show", f"HEAD:{path}")
        return res.stdout if res.returncode == 0 else None
```

`graph.py`：`AifixState` 加 `signals: dict[str, Any]`（`new_state` 里初始化为 `{}`）。

`verify_node`：在 `if verdict is Verdict.BETTER:` **之前**算：

```python
    def _now(p: str) -> str | None:
        """工作区当前内容；补丁把文件删掉时返回 None。"""
        f = worktree_path / p
        return f.read_text(encoding="utf-8") if f.is_file() else None

    touched = state.get("touched") or []
    sig = analyze({p: (wt.file_at_head(p), _now(p)) for p in touched},
                  suspect=(state.get("diagnosis") or {}).get("suspect_file"))
    if not sig.is_empty():
        # 只在有信号时写 fact：一条恒定出现的空 fact 会让 facts.jsonl 变噪音
        for n in sig.removed_public_symbols:
            trace.fact("removed_public_symbol", n)
        for n in sig.new_module_state:
            trace.fact("new_module_state", n)
        if sig.files_outside_suspect:
            trace.fact("files_outside_suspect", sig.files_outside_suspect)
```

并把 `sig` 放进返回的 dict（`"signals": {...}`），三个返回分支都要带上。

**注意**：`verify_node` 里 `wt` 已经存在（`_worktree(state)`）。`diagnosis` 在 `verify_node` 的返回里被清成 `None`，但**读取发生在清空之前**，没问题——实现时确认这一点。

`report.py`：`render_report` 在「合并：」那行之前插入

```python
    sig = state.get("signals") or {}
    if any(sig.values()):
        lines += ["", "## ⚠️ 值得多看一眼", ""]
        if sig.get("removed_public_symbols"):
            lines.append(f"- 补丁删除了公开符号："
                         f"{'、'.join('`%s`' % x for x in sig['removed_public_symbols'])}")
        if sig.get("new_module_state"):
            lines.append(f"- 补丁新增了模块级可变状态："
                         f"{'、'.join('`%s`' % x for x in sig['new_module_state'])}")
        if sig.get("files_outside_suspect"):
            lines.append(f"- 改动落在诊断的嫌疑文件之外："
                         f"{'、'.join('`%s`' % x for x in sig['files_outside_suspect'])}")
        lines += ["",
                  "这些是静态信号，**不改变判定** —— 测试确实转绿了。"
                  "它们只是说：合并之前值得亲眼看一遍这个 diff。"]
```

- [ ] **步骤 4：跑测试确认通过**

`uv run pytest tests/test_delivery.py tests/test_signals_wiring.py tests/test_e2e.py -q` → 全绿。

- [ ] **步骤 5：Commit**

---

### 任务 14：信号进 `TaskResult` 与对比表

**文件：**
- 修改：`src/aifix/eval/runner.py`、`src/aifix/eval/score.py`
- 测试：`tests/test_eval_runner.py`、`tests/test_eval_score.py`

- [ ] **步骤 1：写失败测试**

```python
def test_task_result_counts_signals_from_facts():
    """信号条数从 facts.jsonl 数出来 —— 和 violations 同一条路径。"""


def test_table_has_a_signal_column_and_it_is_a_total_not_an_average():
    """和「越界尝试」一样是总次数：一个任务爆出 7 条信号不该被均值稀释。"""
    table = render_table([summarize([_result(signals=3), _result(signals=4)])])
    assert "| 7 |" in table          # 总数
    assert "| 3.5 |" not in table    # 反向钉死：不是均值
```

`3 + 4 = 7` 而不是 `3 + 2 = 5`：**5 这个数字在这张表里到处都可能出现**（任务数、tokens、越界次数），断言就失去区分度了。7 与均值 3.5 两条一起断言，才同时排除了"没这一列"和"算成了均值"。这类挑选是本计划的硬要求，不是风格问题。

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

- `runner.run_task`：`signals = sum(1 for f in facts if f.get("key") in
  ("removed_public_symbol", "new_module_state", "files_outside_suspect"))`
  —— `files_outside_suspect` 的 value 是列表，按**一条 fact 记一条信号**还是按列表长度记？统一按 fact 条数记，并在注释里写明理由（三类信号各记一条，避免"改了 20 个文件"这种情形把这一列冲爆）。
- `Summary` 加 `signals: int`（总次数，不是均值），`summarize` 里 `sum(r.signals for r in valid)`
- `render_table` 加「可疑信号」列
- 文档：这一列怎么读——**修复成功率高、可疑信号也高，那是规格套利的指纹**。单看任何一列都得不出这个结论。写进 `score.py` 的模块 docstring。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

## 阶段六 · 收尾

### 任务 15：`on_done` 移出锁

**背景**：`eval/runner.py` 的跳过路径在**锁内**调用 `on_done`，正常路径与异常路径都在锁外。`on_done` 是用户回调（CLI 里是 `print`），在锁内调用会把整批调度阻塞在一次 I/O 上，且行为与另外两条路径不一致。

**文件：**
- 修改：`src/aifix/eval/runner.py`
- 测试：`tests/test_eval_runner.py`

- [ ] **步骤 1：写失败测试**

```python
async def test_on_done_is_never_called_while_holding_the_lock():
    """三条路径（正常 / 异常 / 跳过）都必须在锁外回调。

    断言方式：on_done 里去 acquire 同一把锁（用一个能拿到 runner 内部锁的
    构造，或者退一步 —— on_done 里 await asyncio.sleep(0) 之后断言其他任务
    确实推进了）。选一种能真正区分「锁内」与「锁外」的写法，不要写一个
    无论如何都通过的断言。
    """
```

**给实现者的提醒**：这条测试很容易写成恒真。若找不到能真正区分的写法，**在报告里说明并给出你的判断**，不要交一个假装验证过的断言。

- [ ] **步骤 2：跑测试确认失败**

- [ ] **步骤 3：实现**

把跳过分支改成在 `async with lock` 之外调 `on_done`：锁内只决定 `skipped = True`，出锁后统一回调并返回。

- [ ] **步骤 4：跑测试确认通过**

- [ ] **步骤 5：Commit**

---

### 任务 16：端到端验证（不花钱）

**这是一个验证任务。**

- [ ] **步骤 1：全量套件**

`uv run pytest -q` → 预期 288 + 本里程碑新增，全绿，**0 失败**。

- [ ] **步骤 2：真跑 mine**

`uv run aifix mine . --limit 30 --max-tasks 10 --out /tmp/m4-mined.jsonl`
记录耗时与任务数。

- [ ] **步骤 3：真跑 mutate**

`uv run aifix mutate . --max-tasks 5 --out /tmp/m4-mutants.jsonl`
记录耗时与任务数。抽一条 `prepare_task_repo` 后跑全量，确认 `target_test` 真的红。

- [ ] **步骤 4：合并两个任务集，用假模型跑一次 eval**

把两份 jsonl 拼起来，用 scripted client 跑 `run_suite`（写一段一次性脚本，不入库），确认：
- 对比表出现**两行**（mined / mutated），不是一行
- 两列比率都带分数与区间
- 「可疑信号」列存在

- [ ] **步骤 5：把这一轮的实测数字写进报告文件**

---

## 交给 M5 的缺口

本计划不做以下几项，理由见规格 §8：

| 缺口 | 说明 |
|---|---|
| `MavenAdapter` | 验证适配层抽象是否真的成立 |
| `aifix replay` | 消费 `events.jsonl` 逐步重演 |
| SQLite 跨 run 轨迹 | jsonl 撑得住单 suite，跨 suite 跨时间的聚合仍缺一张表 |
| 覆盖率差分 | A 组最贵的一档，需要先有本计划的规模化数据才知道值不值得 |
| 跑那一轮花钱的完整评测 | 外向动作，由用户决定 |
