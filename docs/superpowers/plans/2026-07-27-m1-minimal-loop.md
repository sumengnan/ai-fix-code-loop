# M1 端到端最小闭环 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `aifix run` 在一个真实的 pytest 项目上跑通——红色测试进去，绿色分支出来，主工作区未被触碰。

**架构：** LangGraph 状态图编排六个节点（preflight → baseline → detect → fix → verify → report）；模型接入、工具循环、预算、打转检测全部来自已发布的 `ai-harness-framework`；本项目只写领域层：pytest 适配器、三态判定、worktree 交付、三个新工具。

**技术栈：** Python ≥3.11 · `ai-harness-framework` ≥0.0.1 · `langgraph` ≥1.2 · `langgraph-checkpoint-sqlite` ≥3.1 · pydantic v2 · pytest + pytest-asyncio · uv

**规格：** `docs/superpowers/specs/2026-07-27-ai-fix-code-loop-design.md`

## 与规格的偏差（已确认）

| 项 | 规格位置 | 调整 | 理由 |
|---|---|---|---|
| 回滚 | M2 | 移入 M1 | 三态判定与回滚不可分；无回滚则第二轮在上一轮垃圾上工作 |
| `apply_patch` 拒绝测试目录 | M2 | 移入 M1 | 仅 5 行；留出「删断言蒙混过关」的窗口是危险默认值 |

M2 保留：空 diff 守卫、巨型 diff 守卫、flaky 过滤、连续失败熔断。

## 文件结构

**框架侧**（`../ai-harness-framework`，两处向后兼容改动）

| 文件 | 职责 |
|---|---|
| `src/harness/sandbox/local.py` | 增加 `workspace` 参数：接管既有目录，`close()` 不删 |
| `src/harness/loop/agent_loop.py` | `run()` 增加 `messages` 关键字参数，保留 `user_message` 旧用法 |

**本项目**（`src/aifix/`）

| 文件 | 职责 |
|---|---|
| `adapters/base.py` | `Failure` / `SourceCandidate` / `FailureSet` / `Verdict` / `ProjectAdapter` 协议 |
| `adapters/junit.py` | JUnit XML → `FailureSet`（两个适配器共享） |
| `adapters/pytest_adapter.py` | pytest 的命令、报告位置、test_id 转换、`locate_source` |
| `verify.py` | 三态判定 `compare()` |
| `delivery.py` | worktree 建立/回滚/提交/清理、报告渲染 |
| `tools/search.py` | `GrepTool` |
| `tools/patch.py` | `ApplyPatchTool`（路径围栏 + 测试目录拒绝） |
| `tools/tests.py` | `RunTestsTool`（范围强制） |
| `agents/runner.py` | `consume()`：`AgentLoop` 事件流 → `AgentOutcome` |
| `agents/detector.py` | `Diagnosis` schema + 提示词 + `run_detector()` |
| `agents/fixer.py` | 工具注册 + 提示词 + `run_fixer()` |
| `config.py` | `AifixConfig` |
| `graph.py` | `AifixState` + LangGraph 图装配 |
| `nodes/*.py` | 六个节点，一文件一节点 |
| `cli.py` | `aifix run` |

---

# 阶段 0：框架侧改动

在 `../ai-harness-framework` 中进行。每次改动后 **ai-learning-helper 的完整测试套件必须仍全绿**。

### 任务 1：`LocalSandbox` 支持接管既有目录

**文件：**
- 修改：`../ai-harness-framework/src/harness/sandbox/local.py`
- 测试：`../ai-harness-framework/tests/test_local_sandbox.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_local_sandbox.py` 末尾：

```python
async def test_adopted_workspace_is_used_and_not_deleted(tmp_path):
    """接管既有目录：工作区就是传入的路径，close() 后目录仍在。"""
    (tmp_path / "existing.txt").write_text("kept", encoding="utf-8")
    sb = LocalSandbox(workspace=str(tmp_path))
    await sb.start()
    try:
        assert sb.workspace == str(tmp_path)
        assert await sb.read_file("existing.txt") == "kept"
    finally:
        await sb.close()
    assert tmp_path.exists()
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "kept"


async def test_default_workspace_still_temp_and_deleted():
    """不传 workspace 时行为完全不变：临时目录 + close() 删除。"""
    sb = LocalSandbox()
    await sb.start()
    ws = sb.workspace
    assert ws and ws != ""
    await sb.close()
    import os
    assert not os.path.isdir(ws)


async def test_adopted_workspace_still_confines_paths(tmp_path):
    """接管模式下路径围栏依然生效。"""
    sb = LocalSandbox(workspace=str(tmp_path))
    await sb.start()
    try:
        with pytest.raises(SandboxError):
            await sb.read_file("../../etc/passwd")
    finally:
        await sb.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd ../ai-harness-framework
uv run pytest tests/test_local_sandbox.py -q
```

预期：`test_adopted_workspace_is_used_and_not_deleted` 与 `test_adopted_workspace_still_confines_paths` FAIL，报 `TypeError: LocalSandbox() takes no arguments` 之类。

- [ ] **步骤 3：编写最少实现代码**

把 `src/harness/sandbox/local.py` 的 `__init__` / `start` / `close` 替换为：

```python
class LocalSandbox:
    """本地目录 + 子进程。仅测试/离线开发用——不是安全边界。

    workspace=None：自建临时目录，close() 时删除（默认，行为不变）。
    workspace=路径：接管既有目录，close() **不删**——用于在 git worktree
    这类由调用方管理生命周期的目录上工作。
    """

    def __init__(self, workspace: str | None = None) -> None:
        self._adopted = workspace is not None
        self.workspace = workspace or ""
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if not self._adopted:
            self.workspace = tempfile.mkdtemp(prefix="harness_sbx_")
        self._started = True

    async def close(self) -> None:
        # 接管的目录归调用方所有，绝不删除
        if (not self._adopted and self._started
                and self.workspace and os.path.isdir(self.workspace)):
            shutil.rmtree(self.workspace, ignore_errors=True)
        self._started = False
```

其余方法（`for_language` / `exec` / `write_file` / `write_bytes` / `read_file` / `list_files`）一行不改。

- [ ] **步骤 4：运行测试验证通过**

```bash
cd ../ai-harness-framework
uv run pytest tests/test_local_sandbox.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行框架全量测试**

```bash
uv run pytest -q
```

预期：`660 passed, 3 skipped`（原 657 + 新增 3）。

- [ ] **步骤 6：Commit**

```bash
cd ../ai-harness-framework
git add src/harness/sandbox/local.py tests/test_local_sandbox.py
git commit -m "feat(sandbox): LocalSandbox 支持接管既有目录

workspace=None 时行为完全不变（自建临时目录 + close 时删除）；
传入 workspace 则接管该目录，close() 不删——供调用方在自己管理
生命周期的目录（如 git worktree）上工作。路径围栏不受影响。"
```

---

### 任务 2：`AgentLoop.run()` 支持传入完整初始消息

**文件：**
- 修改：`../ai-harness-framework/src/harness/loop/agent_loop.py:103-107`
- 测试：`../ai-harness-framework/tests/test_loop.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_loop.py` 末尾：

```python
from harness.types import Message, Role


async def test_run_accepts_initial_messages(make_mock, text_turn):
    """传入完整初始消息：全部进入状态，且顺序保持。"""
    seen: list[list] = []

    class _Recording:
        def __init__(self, inner):
            self._inner = inner

        async def stream(self, messages, tools):
            seen.append(list(messages))
            async for c in self._inner.stream(messages, tools):
                yield c

    loop = _build_loop(_Recording(make_mock([text_turn("ok")])))
    initial = [
        Message(role=Role.USER, content="诊断如下"),
        Message(role=Role.ASSISTANT, content="收到"),
        Message(role=Role.USER, content="请修复"),
    ]
    events = [ev async for ev in loop.run(messages=initial)]
    assert isinstance(events[-1], RunFinished)
    # ContextManager 在最前插入 system，其后应为传入的三条
    sent = seen[0]
    assert [m.role for m in sent[1:]] == [Role.USER, Role.ASSISTANT, Role.USER]
    assert [m.content for m in sent[1:]] == ["诊断如下", "收到", "请修复"]


async def test_run_still_accepts_plain_string(make_mock, text_turn):
    """旧用法不变。"""
    loop = _build_loop(make_mock([text_turn("ok")]))
    events = [ev async for ev in loop.run("hi")]
    assert isinstance(events[-1], RunFinished)


async def test_run_requires_exactly_one_input(make_mock, text_turn):
    """两者都不给或都给，都是调用方错误。"""
    loop = _build_loop(make_mock([text_turn("ok")]))
    with pytest.raises(ValueError):
        [ev async for ev in loop.run()]
    with pytest.raises(ValueError):
        [ev async for ev in loop.run("hi", messages=[Message(role=Role.USER, content="x")])]
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd ../ai-harness-framework
uv run pytest tests/test_loop.py -q -k "initial_messages or plain_string or exactly_one"
```

预期：`test_run_accepts_initial_messages` 与 `test_run_requires_exactly_one_input` FAIL（`run()` 不接受 `messages` 关键字参数）。

- [ ] **步骤 3：编写最少实现代码**

把 `src/harness/loop/agent_loop.py` 中的 `run` 方法（现第 103–107 行）替换为：

```python
    async def run(self, user_message: str | None = None, *,
                  messages: list[Message] | None = None) -> AsyncIterator[Event]:
        """启动一次 run。

        user_message：单条用户消息（旧用法，保持不变）。
        messages：完整的初始消息列表——调用方已经组装好上下文时使用
        （如把上一步的结构化诊断作为 assistant/user 轮次一并带入）。
        两者必须且只能给一个。
        """
        if (user_message is None) == (messages is None):
            raise ValueError("run() 需要 user_message 或 messages 之一，且不能同时提供")
        state = RunState(run_id=self._new_run_id())
        if messages is None:
            state.append(Message(role=Role.USER, content=user_message))
        else:
            for m in messages:
                state.append(m)
        async for ev in self._run_from(state, resuming=False):
            yield ev
```

`Message` 与 `Role` 已在该文件顶部导入，无需新增 import。

- [ ] **步骤 4：运行测试验证通过**

```bash
cd ../ai-harness-framework
uv run pytest tests/test_loop.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行框架全量测试 + 原项目兼容性回归**

```bash
cd ../ai-harness-framework && uv run pytest -q
cd ../ai-learning-helper && uv run pytest -q
```

预期：框架 `663 passed, 3 skipped`；ai-learning-helper `1906 passed, 5 skipped`。

- [ ] **步骤 6：Commit 并发版**

```bash
cd ../ai-harness-framework
git add src/harness/loop/agent_loop.py tests/test_loop.py
git commit -m "feat(loop): AgentLoop.run 支持传入完整初始消息

run(user_message) 旧用法完全不变；新增 run(messages=[...]) 供调用方
自行组装上下文（如把结构化诊断作为独立轮次带入）。两者互斥，
同时给或都不给抛 ValueError。"
```

把 `pyproject.toml` 的 `version` 改为 `0.1.0`，然后：

```bash
git add pyproject.toml && git commit -m "chore: 发版 0.1.0"
git tag -f v0.1.0 && git push -f origin master --follow-tags
```

> 后续 aifix 的依赖将写作 `ai-harness-framework>=0.1.0`。若尚未推到 PyPI，用 `[tool.uv.sources]` 的本地 editable 覆盖即可联调。

---

# 阶段 1：数据类型与判定

### 任务 3：项目骨架

**文件：**
- 创建：`pyproject.toml`、`src/aifix/__init__.py`、`tests/conftest.py`

- [ ] **步骤 1：写 `pyproject.toml`**

```toml
[project]
name = "aifix"
version = "0.1.0"
description = "测试失败驱动的自我改进 agentic loop"
requires-python = ">=3.11"
dependencies = [
    "ai-harness-framework>=0.1.0",
    "langgraph>=1.2",
    "langgraph-checkpoint-sqlite>=3.1",
]

[project.scripts]
aifix = "aifix.cli:main"

[tool.uv.sources]
ai-harness-framework = { path = "../ai-harness-framework", editable = true }

[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]
addopts = "--import-mode=importlib"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/aifix"]
```

- [ ] **步骤 2：建空包与共享夹具**

```bash
mkdir -p src/aifix/{adapters,tools,agents,nodes} tests
touch src/aifix/__init__.py src/aifix/adapters/__init__.py \
      src/aifix/tools/__init__.py src/aifix/agents/__init__.py \
      src/aifix/nodes/__init__.py
```

`tests/conftest.py`：

```python
"""共享夹具：一个可复现的、带失败测试的临时 git 仓库。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b
'''

_FIXED = '''def add(a, b):
    return a + b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_identity():
    assert add(0, 0) == 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True).stdout


@pytest.fixture
def buggy_repo(tmp_path: Path) -> Path:
    """一个 pytest 项目：test_add 失败，test_identity 通过。"""
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def fixed_source() -> str:
    return _FIXED
```

`repo.mkdir` 由 `(repo / "tests").mkdir(parents=True)` 顺带创建。

- [ ] **步骤 3：装依赖并确认能 import**

```bash
uv sync --group dev
uv run python -c "import harness, langgraph; from langgraph.checkpoint.sqlite import SqliteSaver; print('ok')"
```

预期：打印 `ok`。若 `SqliteSaver` 导入失败，说明 `langgraph-checkpoint-sqlite` 未装上——检查依赖。

- [ ] **步骤 4：Commit**

```bash
git add pyproject.toml uv.lock src/aifix tests/conftest.py
git commit -m "chore: 项目骨架与测试夹具"
```

---

### 任务 4：核心数据类型

**文件：**
- 创建：`src/aifix/adapters/base.py`
- 测试：`tests/test_adapters_base.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_adapters_base.py`：

```python
from aifix.adapters.base import Failure, FailureSet, SourceCandidate, Verdict


def _f(tid: str) -> Failure:
    return Failure(test_id=tid, classname="c", name="n", message="m", trace="t")


def test_failure_set_ids():
    fs = FailureSet({"a": _f("a"), "b": _f("b")})
    assert fs.ids == {"a", "b"}


def test_failure_set_empty():
    assert FailureSet({}).ids == set()
    assert not FailureSet({}).failures


def test_verdict_values():
    assert Verdict.BETTER.value == "better"
    assert Verdict.SAME.value == "same"
    assert Verdict.WORSE.value == "worse"


def test_source_candidate_fields():
    sc = SourceCandidate(path="calc.py", line=2, frame="add")
    assert (sc.path, sc.line, sc.frame) == ("calc.py", 2, "add")
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_adapters_base.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.adapters.base'`。

- [ ] **步骤 3：编写实现**

`src/aifix/adapters/base.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Failure:
    """一个失败的测试用例。test_id 必须可直接喂回 run_tests。"""
    test_id: str
    classname: str
    name: str
    message: str
    trace: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class SourceCandidate:
    """从栈帧还原出的嫌疑源码位置，按可疑度排序（越靠前越可疑）。"""
    path: str
    line: int
    frame: str


@dataclass(frozen=True)
class FailureSet:
    failures: dict[str, Failure]

    @property
    def ids(self) -> set[str]:
        return set(self.failures)


class Verdict(str, Enum):
    BETTER = "better"
    SAME = "same"
    WORSE = "worse"


class ProjectAdapter(Protocol):
    """把「某种语言的测试工程」翻译成核心循环认识的四个问题 + 一个真活。"""

    name: str

    @staticmethod
    def detect(repo: Path) -> bool: ...

    def full_test_command(self, report_path: str) -> list[str]: ...

    def scoped_test_command(self, test_ids: list[str], report_path: str) -> list[str]: ...

    def report_glob(self) -> str: ...

    def test_dirs(self) -> list[str]: ...

    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_adapters_base.py -q
```

预期：4 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/adapters/base.py tests/test_adapters_base.py
git commit -m "feat(adapters): 核心数据类型与 ProjectAdapter 协议"
```

---

### 任务 5：JUnit XML 解析

**文件：**
- 创建：`src/aifix/adapters/junit.py`
- 测试：`tests/test_junit.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_junit.py`：

```python
from pathlib import Path

from aifix.adapters.junit import parse_junit

_XML = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" errors="1" skipped="0">
    <testcase classname="tests.test_calc" name="test_add" file="tests/test_calc.py" line="4" time="0.01">
      <failure message="assert -1 == 5">Traceback...\nE  assert -1 == 5</failure>
    </testcase>
    <testcase classname="tests.test_calc" name="test_boom" file="tests/test_calc.py" line="9" time="0.01">
      <error message="ZeroDivisionError">Traceback...\nZeroDivisionError</error>
    </testcase>
    <testcase classname="tests.test_calc" name="test_identity" file="tests/test_calc.py" line="8" time="0.01"/>
  </testsuite>
</testsuites>
'''


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "report.xml"
    p.write_text(_XML, encoding="utf-8")
    return p


def test_collects_failures_and_errors(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    assert fs.ids == {"tests.test_calc::test_add", "tests.test_calc::test_boom"}


def test_passing_case_excluded(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    assert "tests.test_calc::test_identity" not in fs.ids


def test_message_and_trace_captured(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    fail = fs.failures["tests.test_calc::test_add"]
    assert fail.message == "assert -1 == 5"
    assert "assert -1 == 5" in fail.trace
    assert fail.file == "tests/test_calc.py"
    assert fail.line == 4


def test_multiple_report_files_merged(tmp_path):
    a = tmp_path / "a.xml"
    a.write_text(_XML, encoding="utf-8")
    b = tmp_path / "b.xml"
    b.write_text(_XML.replace("test_add", "test_other"), encoding="utf-8")
    fs = parse_junit([a, b], lambda c, n, f: f"{c}::{n}")
    assert "tests.test_calc::test_other" in fs.ids
    assert len(fs.ids) == 3


def test_missing_file_is_ignored(tmp_path):
    fs = parse_junit([tmp_path / "nope.xml"], lambda c, n, f: f"{c}::{n}")
    assert fs.ids == set()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_junit.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.adapters.junit'`。

- [ ] **步骤 3：编写实现**

`src/aifix/adapters/junit.py`：

```python
"""JUnit XML → FailureSet。pytest / Maven Surefire / Gradle / Jest 共享此解析。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

from .base import Failure, FailureSet

MakeTestId = Callable[[str, str, str | None], str]


def parse_junit(paths: Iterable[Path], make_test_id: MakeTestId) -> FailureSet:
    """解析一批 JUnit XML，收集所有 <failure> 与 <error> 的用例。

    make_test_id 由适配器提供：报告里的 classname 未必能直接拿去重跑
    （pytest 给的是点分模块名，重跑要的是文件路径形式）。
    """
    failures: dict[str, Failure] = {}
    for path in paths:
        if not Path(path).is_file():
            continue                      # 报告缺失（如测试进程崩溃）不算解析错误
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            # 注意：Element 无子元素时为 falsy，必须显式与 None 比较
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if bad is None:
                continue
            classname = case.get("classname", "")
            name = case.get("name", "")
            file = case.get("file")
            raw_line = case.get("line")
            test_id = make_test_id(classname, name, file)
            failures[test_id] = Failure(
                test_id=test_id,
                classname=classname,
                name=name,
                message=bad.get("message", ""),
                trace=(bad.text or ""),
                file=file,
                line=int(raw_line) if raw_line and raw_line.isdigit() else None,
            )
    return FailureSet(failures)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_junit.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/adapters/junit.py tests/test_junit.py
git commit -m "feat(adapters): JUnit XML 解析（failure 与 error 均收集）"
```

---

### 任务 6：三态判定

**文件：**
- 创建：`src/aifix/verify.py`
- 测试：`tests/test_verify.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_verify.py`：

```python
from aifix.adapters.base import Failure, FailureSet, Verdict
from aifix.verify import compare


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


def test_target_fixed_is_better():
    assert compare(_fs("a", "b"), _fs("b"), "a") is Verdict.BETTER


def test_nothing_changed_is_same():
    assert compare(_fs("a", "b"), _fs("a", "b"), "a") is Verdict.SAME


def test_new_failure_is_worse():
    assert compare(_fs("a"), _fs("a", "c"), "a") is Verdict.WORSE


def test_target_fixed_but_regression_is_worse():
    """核心约束：即使目标修好了，引入任何新失败一律 WORSE。"""
    assert compare(_fs("a", "b"), _fs("b", "c"), "a") is Verdict.WORSE


def test_other_failure_fixed_but_target_not_is_same():
    """顺手修好别的、目标没修好 —— 不算 BETTER。"""
    assert compare(_fs("a", "b"), _fs("a"), "a") is Verdict.SAME


def test_all_fixed_is_better():
    assert compare(_fs("a", "b"), _fs(), "a") is Verdict.BETTER
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_verify.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.verify'`。

- [ ] **步骤 3：编写实现**

`src/aifix/verify.py`：

```python
"""三态判定：系统里唯一有资格说「修好了」的地方，且不含任何 LLM。"""
from __future__ import annotations

from .adapters.base import FailureSet, Verdict


def compare(baseline: FailureSet, current: FailureSet, target: str) -> Verdict:
    """把两次测试结果的差异归结为 BETTER / SAME / WORSE。

    new 的判断在最前：**即使目标用例修好了，只要引入任何新失败一律 WORSE**。
    比「净改善」更保守，但这正是敢在真实 repo 上跑的前提。
    """
    new = current.ids - baseline.ids
    if new:
        return Verdict.WORSE
    if target in (baseline.ids - current.ids):
        return Verdict.BETTER
    return Verdict.SAME
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_verify.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/verify.py tests/test_verify.py
git commit -m "feat(verify): 三态判定 —— 引入回归一律 WORSE"
```

---

### 任务 7：`PytestAdapter`

**文件：**
- 创建：`src/aifix/adapters/pytest_adapter.py`
- 测试：`tests/test_pytest_adapter.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_pytest_adapter.py`：

```python
from pathlib import Path

from aifix.adapters.base import Failure
from aifix.adapters.pytest_adapter import PytestAdapter


def test_detect_by_pytest_ini(buggy_repo):
    assert PytestAdapter.detect(buggy_repo) is True


def test_detect_rejects_plain_dir(tmp_path):
    assert PytestAdapter.detect(tmp_path) is False


def test_full_command_includes_junitxml():
    cmd = PytestAdapter().full_test_command("/tmp/r.xml")
    assert "--junitxml=/tmp/r.xml" in cmd
    assert cmd[0] in ("python", "python3", "pytest")


def test_scoped_command_contains_ids():
    cmd = PytestAdapter().scoped_test_command(
        ["tests/test_calc.py::test_add"], "/tmp/r.xml")
    assert "tests/test_calc.py::test_add" in cmd


def test_make_test_id_prefers_file_path():
    """报告里的 classname 是点分模块名，重跑要的是文件路径形式。"""
    tid = PytestAdapter().make_test_id(
        "tests.test_calc", "test_add", "tests/test_calc.py")
    assert tid == "tests/test_calc.py::test_add"


def test_make_test_id_falls_back_to_classname():
    tid = PytestAdapter().make_test_id("tests.test_calc", "test_add", None)
    assert tid == "tests/test_calc.py::test_add"


def test_test_dirs():
    assert "tests" in PytestAdapter().test_dirs()


def test_locate_source_picks_deepest_repo_frame(buggy_repo):
    trace = (
        'Traceback (most recent call last):\n'
        f'  File "{buggy_repo}/tests/test_calc.py", line 5, in test_add\n'
        '    assert add(2, 3) == 5\n'
        f'  File "{buggy_repo}/calc.py", line 2, in add\n'
        '    return a - b\n'
        '  File "/usr/lib/python3.13/site-packages/_pytest/x.py", line 1, in run\n'
    )
    fail = Failure(test_id="t", classname="c", name="n", message="m", trace=trace)
    cands = PytestAdapter().locate_source(fail, buggy_repo)
    assert cands[0].path == "calc.py"        # 最深的 repo 内帧
    assert cands[0].line == 2
    assert cands[0].frame == "add"
    assert all("site-packages" not in c.path for c in cands)


def test_locate_source_empty_when_no_repo_frames(buggy_repo):
    fail = Failure(test_id="t", classname="c", name="n", message="m",
                   trace='File "/usr/lib/python3.13/os.py", line 1, in x\n')
    assert PytestAdapter().locate_source(fail, buggy_repo) == []
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_pytest_adapter.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.adapters.pytest_adapter'`。

- [ ] **步骤 3：编写实现**

`src/aifix/adapters/pytest_adapter.py`：

```python
from __future__ import annotations

import re
import sys
from pathlib import Path

from .base import Failure, SourceCandidate

# 形如：  File "/abs/path/calc.py", line 2, in add
_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')

_MARKERS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py")


class PytestAdapter:
    name = "pytest"

    @staticmethod
    def detect(repo: Path) -> bool:
        if any((repo / m).is_file() for m in _MARKERS):
            return True
        return (repo / "tests").is_dir()

    def full_test_command(self, report_path: str) -> list[str]:
        return [sys.executable, "-m", "pytest", "-q",
                f"--junitxml={report_path}", "-p", "no:cacheprovider"]

    def scoped_test_command(self, test_ids: list[str], report_path: str) -> list[str]:
        return [sys.executable, "-m", "pytest", "-q",
                f"--junitxml={report_path}", "-p", "no:cacheprovider", *test_ids]

    def report_glob(self) -> str:
        return ".aifix-report.xml"

    def test_dirs(self) -> list[str]:
        return ["tests", "test"]

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """pytest 重跑要的是 `路径::用例`，而报告给的 classname 是点分模块名。"""
        path = file or (classname.replace(".", "/") + ".py")
        return f"{path}::{name}"

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 traceback 抽出 repo 内部帧，最深的排最前。"""
        repo_real = str(Path(repo).resolve())
        out: list[SourceCandidate] = []
        for m in _FRAME.finditer(failure.trace):
            raw = m.group("path")
            try:
                real = str(Path(raw).resolve())
            except OSError:
                continue
            if not (real == repo_real or real.startswith(repo_real + "/")):
                continue
            if "site-packages" in real or "/dist-packages/" in real:
                continue
            out.append(SourceCandidate(
                path=str(Path(real).relative_to(repo_real)),
                line=int(m.group("line")),
                frame=m.group("fn"),
            ))
        out.reverse()          # traceback 由浅入深，最深的最可疑
        return out
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_pytest_adapter.py -q
```

预期：9 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/adapters/pytest_adapter.py tests/test_pytest_adapter.py
git commit -m "feat(adapters): PytestAdapter

locate_source 从 traceback 提取 repo 内部帧、最深的排最前；
make_test_id 把报告的点分 classname 转成可重跑的路径形式。"
```

---

# 阶段 2：工具

三个工具都以 `harness.sandbox.base.Sandbox` 为唯一出口，路径围栏由框架的 `resolve_in_workspace()` 提供。

### 任务 8：`GrepTool`

**文件：**
- 创建：`src/aifix/tools/search.py`
- 测试：`tests/test_tool_grep.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_tool_grep.py`：

```python
import pytest
from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolExecutor, ToolRegistry
from harness.types import ToolCall

from aifix.tools.search import GrepTool


@pytest.fixture
async def executor(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    reg = ToolRegistry()
    reg.register(GrepTool(sb))
    yield ToolExecutor(reg, max_chars=8000)
    await sb.close()


async def test_finds_match(executor):
    r = await executor.execute(ToolCall(id="1", name="grep",
                                        arguments={"pattern": "def add"}))
    assert not r.is_error
    assert "calc.py" in r.content


async def test_no_match_reports_clearly(executor):
    r = await executor.execute(ToolCall(id="1", name="grep",
                                        arguments={"pattern": "zzz_not_here"}))
    assert not r.is_error
    assert "无匹配" in r.content


async def test_max_results_capped(executor):
    r = await executor.execute(ToolCall(
        id="1", name="grep",
        arguments={"pattern": "def", "max_results": 1}))
    assert not r.is_error
    assert len([ln for ln in r.content.splitlines() if ":" in ln]) <= 1


async def test_path_escape_rejected(executor):
    r = await executor.execute(ToolCall(
        id="1", name="grep",
        arguments={"pattern": "root", "path": "../../etc"}))
    assert r.is_error
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_tool_grep.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.tools.search'`。

- [ ] **步骤 3：编写实现**

`src/aifix/tools/search.py`：

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, resolve_in_workspace
from harness.tools.base import Tool, ToolError
from harness.tools.builtins._sandbox_util import truncate


class GrepTool(Tool):
    name = "grep"
    description = ("在工作区内按正则搜索代码，返回 `文件:行号:内容`。"
                   "底层是 git grep：自动跳过 .gitignore 里的路径。")

    class Params(BaseModel):
        pattern: str = Field(description="正则表达式")
        path: str = Field(default=".", description="限定搜索的子路径")
        max_results: int = Field(default=50, ge=1, le=200)

    def __init__(self, sandbox: Sandbox, timeout: float = 30.0,
                 max_chars: int = 8000) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._max_chars = max_chars

    async def run(self, params: "GrepTool.Params") -> str:
        # 路径围栏：逃逸抛 SandboxError，由 ToolExecutor 兜成 is_error
        resolve_in_workspace(self._sandbox.workspace, params.path)
        res = await self._sandbox.exec(
            ["git", "grep", "-n", "-I", "-E", params.pattern, "--", params.path],
            self._timeout)
        # git grep：0=有匹配，1=无匹配，其余为真错误
        if res.exit_code == 1 and not res.stderr.strip():
            return "无匹配。"
        if res.exit_code not in (0, 1):
            raise ToolError(f"搜索失败：{res.stderr.strip() or res.stdout.strip()}")
        lines = res.stdout.splitlines()[: params.max_results]
        more = "" if len(res.stdout.splitlines()) <= params.max_results else \
            f"\n…（已截断到前 {params.max_results} 条）"
        return truncate("\n".join(lines) + more, self._max_chars)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_tool_grep.py -q
```

预期：4 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/tools/search.py tests/test_tool_grep.py
git commit -m "feat(tools): GrepTool（git grep + 路径围栏 + 结果上限）"
```

---

### 任务 9：`ApplyPatchTool`

**文件：**
- 创建：`src/aifix/tools/patch.py`
- 测试：`tests/test_tool_patch.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_tool_patch.py`：

```python
import pytest
from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolExecutor, ToolRegistry
from harness.types import ToolCall

from aifix.tools.patch import ApplyPatchTool

_GOOD = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

_TOUCHES_TEST = """--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -3,4 +3,4 @@
 def test_add():
-    assert add(2, 3) == 5
+    assert True
"""

_BAD_CONTEXT = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a * b
+    return a + b
"""

_ESCAPES = """--- a/../../evil.py
+++ b/../../evil.py
@@ -0,0 +1 @@
+pwned
"""


@pytest.fixture
async def executor(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    reg = ToolRegistry()
    reg.register(ApplyPatchTool(sb, test_dirs=["tests"]))
    yield ToolExecutor(reg, max_chars=8000), buggy_repo
    await sb.close()


async def test_applies_valid_patch(executor):
    ex, repo = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _GOOD}))
    assert not r.is_error, r.content
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")


async def test_rejects_test_file_edit(executor):
    ex, repo = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _TOUCHES_TEST}))
    assert r.is_error
    assert "测试" in r.content
    assert "assert True" not in (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")


async def test_bad_context_returns_git_error(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _BAD_CONTEXT}))
    assert r.is_error
    assert "patch" in r.content.lower() or "apply" in r.content.lower()


async def test_path_escape_rejected(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _ESCAPES}))
    assert r.is_error


async def test_no_temp_file_left_behind(executor):
    ex, repo = executor
    await ex.execute(ToolCall(id="1", name="apply_patch",
                              arguments={"diff": _GOOD}))
    assert not (repo / ".aifix_patch.diff").exists()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_tool_patch.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.tools.patch'`。

- [ ] **步骤 3：编写实现**

`src/aifix/tools/patch.py`：

```python
from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, SandboxError, resolve_in_workspace
from harness.tools.base import Tool, ToolError

# 取 diff 里的目标路径：`--- a/x.py` / `+++ b/x.py`，忽略 /dev/null
_TARGET = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(?P<path>\S+)", re.M)

_PATCH_FILE = ".aifix_patch.diff"


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "对工作区文件应用 unified diff。只接 diff，不接整文件覆写；"
        "新建文件用 /dev/null 作为源。打不上时会返回 git 的具体报错，"
        "据此修正后重试。不允许修改测试文件。")

    class Params(BaseModel):
        diff: str = Field(description="标准 unified diff，须含 --- / +++ 文件头")

    def __init__(self, sandbox: Sandbox, test_dirs: list[str],
                 timeout: float = 60.0) -> None:
        self._sandbox = sandbox
        self._test_dirs = [d.strip("/") for d in test_dirs]
        self._timeout = timeout

    def _targets(self, diff: str) -> list[str]:
        seen: list[str] = []
        for m in _TARGET.finditer(diff):
            p = m.group("path")
            if p == "/dev/null" or p in seen:
                continue
            seen.append(p)
        return seen

    def _guard(self, targets: list[str]) -> None:
        if not targets:
            raise ToolError("diff 里没有找到 --- / +++ 文件头，无法确定要改哪个文件。")
        for p in targets:
            parts = PurePosixPath(p).parts
            if ".git" in parts:
                raise ToolError(f"拒绝修改 .git 目录下的文件：{p}")
            if parts and parts[0] in self._test_dirs:
                raise ToolError(
                    f"拒绝修改测试文件：{p}。"
                    "请修改源码使测试通过，而不是修改测试本身。")
            # 路径围栏：逃逸工作区抛 SandboxError
            resolve_in_workspace(self._sandbox.workspace, p)

    async def run(self, params: "ApplyPatchTool.Params") -> str:
        try:
            self._guard(self._targets(params.diff))
        except SandboxError as e:
            raise ToolError(str(e))

        body = params.diff if params.diff.endswith("\n") else params.diff + "\n"
        await self._sandbox.write_file(_PATCH_FILE, body)
        try:
            check = await self._sandbox.exec(
                ["git", "apply", "--check", _PATCH_FILE], self._timeout)
            if check.exit_code != 0:
                raise ToolError(
                    "补丁无法应用（git apply --check 失败）："
                    f"{check.stderr.strip() or check.stdout.strip()}\n"
                    "通常说明你对文件当前内容的理解有误，"
                    "请先 read_file 确认后重新生成 diff。")
            applied = await self._sandbox.exec(
                ["git", "apply", _PATCH_FILE], self._timeout)
            if applied.exit_code != 0:
                raise ToolError(
                    f"补丁应用失败：{applied.stderr.strip() or applied.stdout.strip()}")
            stat = await self._sandbox.exec(
                ["git", "diff", "--stat"], self._timeout)
            return "补丁已应用。当前改动：\n" + (stat.stdout.strip() or "（无）")
        finally:
            # 临时文件必须清掉：它是未跟踪文件，留着会干扰后续判断
            await self._sandbox.exec(["rm", "-f", _PATCH_FILE], 10.0)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_tool_patch.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/tools/patch.py tests/test_tool_patch.py
git commit -m "feat(tools): ApplyPatchTool

只接 unified diff；应用前逐个校验目标路径——拒绝 .git、拒绝测试目录、
拒绝逃逸工作区；git apply --check 干跑失败时把 git 原话喂回让模型自纠正。"
```

---

### 任务 10：`RunTestsTool`

**文件：**
- 创建：`src/aifix/tools/tests.py`
- 测试：`tests/test_tool_runtests.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_tool_runtests.py`：

```python
import pytest
from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolExecutor, ToolRegistry
from harness.types import ToolCall

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.tools.tests import RunTestsTool

_KNOWN = {"tests/test_calc.py::test_add", "tests/test_calc.py::test_identity"}


@pytest.fixture
async def executor(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    reg = ToolRegistry()
    reg.register(RunTestsTool(sb, PytestAdapter(), known_ids=_KNOWN))
    yield ToolExecutor(reg, max_chars=8000), buggy_repo
    await sb.close()


async def test_failing_test_reports_failure(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(
        id="1", name="run_tests",
        arguments={"test_ids": ["tests/test_calc.py::test_add"]}))
    assert "1 failed" in r.content or "failed" in r.content


async def test_passing_test_reports_pass(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(
        id="1", name="run_tests",
        arguments={"test_ids": ["tests/test_calc.py::test_identity"]}))
    assert not r.is_error
    assert "passed" in r.content


async def test_unknown_id_rejected(executor):
    """范围强制：不在 baseline 已知集合里的 id 一律拒绝。"""
    ex, _ = executor
    r = await ex.execute(ToolCall(
        id="1", name="run_tests",
        arguments={"test_ids": ["tests/test_calc.py::test_nope"]}))
    assert r.is_error
    assert "未知" in r.content


async def test_empty_ids_rejected_by_schema(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(id="1", name="run_tests",
                                  arguments={"test_ids": []}))
    assert r.is_error
    assert "校验" in r.content


async def test_too_many_ids_rejected_by_schema(executor):
    ex, _ = executor
    r = await ex.execute(ToolCall(
        id="1", name="run_tests",
        arguments={"test_ids": ["a", "b", "c", "d", "e", "f"]}))
    assert r.is_error


async def test_report_file_cleaned_up(executor):
    ex, repo = executor
    await ex.execute(ToolCall(
        id="1", name="run_tests",
        arguments={"test_ids": ["tests/test_calc.py::test_identity"]}))
    assert not (repo / ".aifix-scoped.xml").exists()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_tool_runtests.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.tools.tests'`。

- [ ] **步骤 3：编写实现**

`src/aifix/tools/tests.py`：

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolError
from harness.tools.builtins._sandbox_util import format_exec

from ..adapters.base import ProjectAdapter

_SCOPED_REPORT = ".aifix-scoped.xml"


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "跑指定的测试用例，用于验证你的改动是否让目标用例转绿。"
        "只能跑当前失败列表里的用例，一次最多 5 个；不能跑全量。")

    class Params(BaseModel):
        test_ids: list[str] = Field(
            min_length=1, max_length=5,
            description="测试标识，必须来自当前的失败用例列表")

    def __init__(self, sandbox: Sandbox, adapter: ProjectAdapter,
                 known_ids: set[str], timeout: float = 300.0,
                 max_chars: int = 8000) -> None:
        self._sandbox = sandbox
        self._adapter = adapter
        self._known = set(known_ids)
        self._timeout = timeout
        self._max_chars = max_chars

    async def run(self, params: "RunTestsTool.Params") -> str:
        unknown = [t for t in params.test_ids if t not in self._known]
        if unknown:
            raise ToolError(
                f"未知的测试标识：{unknown}。"
                f"只能跑当前失败列表中的用例：{sorted(self._known)}")
        cmd = self._adapter.scoped_test_command(params.test_ids, _SCOPED_REPORT)
        try:
            res = await self._sandbox.exec(cmd, self._timeout)
        finally:
            await self._sandbox.exec(["rm", "-f", _SCOPED_REPORT], 10.0)
        # 测试失败不是工具失败：结果原样回给模型判断，不抛 ToolError
        return format_exec(res, self._max_chars)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_tool_runtests.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/tools/tests.py tests/test_tool_runtests.py
git commit -m "feat(tools): RunTestsTool（范围强制：只能跑已知失败用例，上限 5 个）

测试失败不抛 ToolError —— 那是给模型看的信号，不是工具故障。"
```

---

# 阶段 3：Agents

### 任务 11：事件流消费

**文件：**
- 创建：`src/aifix/agents/runner.py`
- 测试：`tests/test_agent_runner.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_agent_runner.py`：

```python
from harness.events import (
    ModelUsage, RunError, RunFinished, RunStarted, TextDelta,
)
from harness.types import Message, Role
from harness.usage import Usage

from aifix.agents.runner import AgentOutcome, consume


async def _stream(events):
    for e in events:
        yield e


async def test_collects_text_and_usage():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        TextDelta(text="前"),
        TextDelta(text="后"),
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.02,
                   attempts=1, latency_ms=12.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="前后")),
    ]))
    assert out.text == "前后"
    assert out.tokens == 15
    assert out.cost_usd == 0.02
    assert out.error is None
    assert out.ok is True


async def test_captures_error():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunError(error="达到 max_steps 上限 (25)"),
    ]))
    assert out.error == "达到 max_steps 上限 (25)"
    assert out.ok is False


async def test_accumulates_usage_across_steps():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.01,
                   attempts=1, latency_ms=1.0, model="m"),
        ModelUsage(usage=Usage(20, 10, 30), cost_usd=0.03,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert out.tokens == 45
    assert abs(out.cost_usd - 0.04) < 1e-9


async def test_none_cost_treated_as_zero():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=None,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert out.cost_usd == 0.0


async def test_events_are_retained_for_tracing():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert len(out.events) == 2
    assert isinstance(out.events[0], RunStarted)
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_agent_runner.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.agents.runner'`。

- [ ] **步骤 3：编写实现**

`src/aifix/agents/runner.py`：

```python
"""把 AgentLoop 的异步事件流收敛成一个供 LangGraph 节点使用的结果对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from harness.events import Event, ModelUsage, RunError, TextDelta


@dataclass
class AgentOutcome:
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    events: list[Event] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


async def consume(stream: AsyncIterator[Event]) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用（M2 落 events.jsonl）。"""
    parts: list[str] = []
    out = AgentOutcome()
    async for ev in stream:
        out.events.append(ev)
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ModelUsage):
            out.tokens += ev.usage.total_tokens
            out.cost_usd += ev.cost_usd or 0.0
        elif isinstance(ev, RunError):
            out.error = ev.error
    out.text = "".join(parts)
    return out
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_agent_runner.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/agents/runner.py tests/test_agent_runner.py
git commit -m "feat(agents): consume() 把 AgentLoop 事件流收敛为 AgentOutcome"
```

---

### 任务 12：Detector

**文件：**
- 创建：`src/aifix/agents/detector.py`
- 测试：`tests/test_detector.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_detector.py`：

```python
import json

from harness.llm.base import StreamChunk

from aifix.adapters.base import Failure, SourceCandidate
from aifix.agents.detector import Diagnosis, build_prompt, parse_diagnosis

_FAILURE = Failure(
    test_id="tests/test_calc.py::test_add", classname="tests.test_calc",
    name="test_add", message="assert -1 == 5", trace="File ...\nE assert -1 == 5")
_CANDS = [SourceCandidate(path="calc.py", line=2, frame="add")]


def test_prompt_contains_test_id_message_and_candidates():
    p = build_prompt(_FAILURE, _CANDS)
    assert "tests/test_calc.py::test_add" in p
    assert "assert -1 == 5" in p
    assert "calc.py:2" in p


def test_prompt_handles_empty_candidates():
    p = build_prompt(_FAILURE, [])
    assert "（未能从栈帧定位到 repo 内的源码）" in p


def test_parse_valid_json():
    raw = json.dumps({
        "suspect_file": "calc.py", "suspect_lines": [1, 3],
        "root_cause": "减号写成了加号", "fix_strategy": "改回 a + b",
        "confidence": "high",
    })
    d = parse_diagnosis(raw)
    assert isinstance(d, Diagnosis)
    assert d.suspect_file == "calc.py"
    assert d.suspect_lines == (1, 3)
    assert d.confidence == "high"


def test_parse_tolerates_missing_optional_lines():
    raw = json.dumps({
        "suspect_file": "calc.py", "root_cause": "x",
        "fix_strategy": "y", "confidence": "low",
    })
    assert parse_diagnosis(raw).suspect_lines is None


def test_parse_returns_none_on_garbage():
    """解析失败是降级信号，不是异常 —— 上层改为把原始 traceback 交给 Fixer。"""
    assert parse_diagnosis("这不是 JSON") is None


def test_parse_returns_none_on_schema_mismatch():
    assert parse_diagnosis(json.dumps({"suspect_file": "calc.py"})) is None
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_detector.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.agents.detector'`。

- [ ] **步骤 3：编写实现**

`src/aifix/agents/detector.py`：

```python
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from ..adapters.base import Failure, SourceCandidate

SYSTEM_PROMPT = """你是一个代码缺陷定位专家。

给你一个失败的测试用例和从栈帧还原出的嫌疑源码位置，你要判断真正的缺陷在哪、为什么。
你没有任何工具，只能依据给出的信息推断。

只输出一个 JSON 对象，字段如下：
- suspect_file: 字符串，最可疑的源码文件（相对 repo 根的路径）
- suspect_lines: [起始行, 结束行] 或 null
- root_cause: 字符串，缺陷的根本原因
- fix_strategy: 字符串，修复思路（不要写出完整代码）
- confidence: "high" | "medium" | "low"

注意：栈帧最深处未必是缺陷所在，它可能只是断言失败的位置。"""


class Diagnosis(BaseModel):
    suspect_file: str
    suspect_lines: tuple[int, int] | None = None
    root_cause: str
    fix_strategy: str
    confidence: Literal["high", "medium", "low"]


def build_prompt(failure: Failure, candidates: list[SourceCandidate]) -> str:
    if candidates:
        cand_text = "\n".join(
            f"  {i + 1}. {c.path}:{c.line}  在 {c.frame}()"
            for i, c in enumerate(candidates))
    else:
        cand_text = "  （未能从栈帧定位到 repo 内的源码）"
    return (
        f"失败用例：{failure.test_id}\n"
        f"断言信息：{failure.message}\n\n"
        f"嫌疑位置（按可疑度排序，最深的栈帧在前）：\n{cand_text}\n\n"
        f"完整 traceback：\n{failure.trace}\n")


def parse_diagnosis(raw: str) -> Diagnosis | None:
    """解析失败返回 None —— 这是降级信号，调用方改为把原始 traceback 交给 Fixer。"""
    try:
        return Diagnosis.model_validate_json(raw)
    except ValidationError:
        pass
    # 有些端点会在 JSON 外包一层围栏或解释文字，退一步尝试抽取首个对象
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return Diagnosis.model_validate(json.loads(raw[start:end + 1]))
    except (ValidationError, json.JSONDecodeError):
        return None
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_detector.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/agents/detector.py tests/test_detector.py
git commit -m "feat(agents): Detector 提示词、Diagnosis schema 与容错解析

解析失败返回 None 作为降级信号，不抛异常 —— 流程不因格式问题中断。"
```

---

### 任务 13：Fixer

**文件：**
- 创建：`src/aifix/agents/fixer.py`
- 测试：`tests/test_fixer.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_fixer.py`：

```python
from harness.sandbox.local import LocalSandbox

from aifix.adapters.base import Failure
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.agents.detector import Diagnosis
from aifix.agents.fixer import build_initial_messages, build_registry

_FAILURE = Failure(
    test_id="tests/test_calc.py::test_add", classname="tests.test_calc",
    name="test_add", message="assert -1 == 5", trace="E assert -1 == 5")
_DIAG = Diagnosis(suspect_file="calc.py", suspect_lines=(1, 2),
                  root_cause="减号应为加号", fix_strategy="改回 a + b",
                  confidence="high")


async def test_registry_exposes_exactly_five_tools(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids={_FAILURE.test_id})
        assert {t.name for t in reg.tools()} == {
            "read_file", "list_files", "grep", "apply_patch", "run_tests"}
    finally:
        await sb.close()


async def test_registry_has_no_shell(buggy_repo):
    """关键约束：能力面是白名单，绝不注册 shell。"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids=set())
        assert reg.get("run_shell") is None
        assert reg.get("run_python") is None
    finally:
        await sb.close()


def test_initial_messages_include_diagnosis():
    msgs = build_initial_messages(_FAILURE, _DIAG)
    blob = "\n".join(str(m.content) for m in msgs)
    assert "calc.py" in blob
    assert "减号应为加号" in blob
    assert _FAILURE.test_id in blob


def test_initial_messages_degrade_without_diagnosis():
    msgs = build_initial_messages(_FAILURE, None)
    blob = "\n".join(str(m.content) for m in msgs)
    assert "E assert -1 == 5" in blob
    assert msgs, "降级时仍须给出可用的初始消息"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_fixer.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.agents.fixer'`。

- [ ] **步骤 3：编写实现**

`src/aifix/agents/fixer.py`：

```python
from __future__ import annotations

from harness.sandbox.base import Sandbox
from harness.tools.base import ToolRegistry
from harness.tools.builtins.fs_tools import ListFilesTool, ReadFileTool
from harness.types import Message, Role

from ..adapters.base import Failure, ProjectAdapter
from ..tools.patch import ApplyPatchTool
from ..tools.search import GrepTool
from ..tools.tests import RunTestsTool
from .detector import Diagnosis

SYSTEM_PROMPT = """你是一个修复代码缺陷的工程师。工作区是一个 git worktree，你的改动被隔离在这里。

可用工具：
- read_file / list_files：查看代码
- grep：按正则搜索
- apply_patch：应用 unified diff（唯一的修改手段）
- run_tests：跑目标失败用例，验证你的改动

工作方式：
1. 先 read_file 确认你要改的文件当前的真实内容——不要凭记忆写 diff。
2. 用 apply_patch 提交最小的改动。只改必要的行，不要重写整个文件。
3. 用 run_tests 验证目标用例是否转绿。
4. 转绿后就停下来给出简短说明；没转绿就根据输出继续调整。

约束：
- 不能修改测试文件。让测试通过的唯一正确方式是修源码。
- 没有 shell、没有网络、不能装依赖。
- 你必须真的做出修改。只说"已修复"而没有调用 apply_patch 是无效的。"""


def build_registry(sandbox: Sandbox, adapter: ProjectAdapter,
                   known_ids: set[str]) -> ToolRegistry:
    """Fixer 的能力面：白名单，五个工具，没有 shell。"""
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    reg.register(ApplyPatchTool(sandbox, test_dirs=adapter.test_dirs()))
    reg.register(RunTestsTool(sandbox, adapter, known_ids=known_ids))
    return reg


def build_initial_messages(failure: Failure,
                           diagnosis: Diagnosis | None) -> list[Message]:
    """把失败信息与诊断组装成 Fixer 的初始上下文。

    diagnosis 为 None 时降级：直接把原始 traceback 交给 Fixer 自行判断。
    """
    if diagnosis is None:
        body = (
            f"请修复这个失败的测试：{failure.test_id}\n\n"
            f"断言信息：{failure.message}\n\n"
            f"完整 traceback：\n{failure.trace}\n\n"
            "（自动定位未能给出可用诊断，请自行从 traceback 判断缺陷位置。）")
    else:
        lines = (f"{diagnosis.suspect_lines[0]}-{diagnosis.suspect_lines[1]}"
                 if diagnosis.suspect_lines else "未知")
        body = (
            f"请修复这个失败的测试：{failure.test_id}\n\n"
            f"断言信息：{failure.message}\n\n"
            f"定位分析（置信度 {diagnosis.confidence}）：\n"
            f"  嫌疑文件：{diagnosis.suspect_file}\n"
            f"  嫌疑行号：{lines}\n"
            f"  根本原因：{diagnosis.root_cause}\n"
            f"  修复思路：{diagnosis.fix_strategy}\n\n"
            "这份分析仅供参考，请自己读代码确认后再动手。")
    return [Message(role=Role.USER, content=body)]
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_fixer.py -q
```

预期：4 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/agents/fixer.py tests/test_fixer.py
git commit -m "feat(agents): Fixer 工具注册与初始上下文组装

能力面白名单五个工具，显式不注册 shell；诊断缺失时降级为
直接交付原始 traceback。"
```

---

# 阶段 4：交付、图与端到端

### 任务 14：worktree 交付

**文件：**
- 创建：`src/aifix/delivery.py`
- 测试：`tests/test_delivery.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_delivery.py`：

```python
import subprocess

import pytest

from aifix.delivery import Worktree, ensure_clean


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def test_ensure_clean_passes_on_clean_repo(buggy_repo):
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_ensure_clean_raises_on_dirty_repo(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="工作区不干净"):
        ensure_clean(buggy_repo)


def test_ensure_clean_ignores_aifix_dir(buggy_repo):
    """.aifix/ 是我们自己的产物目录，不能让它把第二次运行卡住。"""
    (buggy_repo / ".aifix" / "runs" / "old").mkdir(parents=True)
    (buggy_repo / ".aifix" / "runs" / "old" / "x.txt").write_text("x", encoding="utf-8")
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_worktree_created_on_new_branch(buggy_repo):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.path.is_dir()
        assert (wt.path / "calc.py").is_file()
        assert wt.branch == "aifix/abc123"
        branches = _git(buggy_repo, "branch", "--list", "aifix/abc123")
        assert "aifix/abc123" in branches


def test_main_worktree_untouched(buggy_repo, fixed_source):
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original


def test_rollback_discards_changes(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.rollback()
        assert "a - b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_commit_keeps_changes_and_rollback_after_is_noop(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.commit("fix: test_add")
        wt.rollback()
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_has_changes_reflects_working_tree(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.has_changes() is False
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        assert wt.has_changes() is True
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_delivery.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.delivery'`。

- [ ] **步骤 3：编写实现**

`src/aifix/delivery.py`：

```python
"""worktree 隔离：agent 的一切改动都发生在这里，主工作区绝不被触碰。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True)


def ensure_clean(repo: Path) -> None:
    """主工作区必须干净——否则无法区分哪些改动是 agent 造成的。

    `.aifix/` 例外：worktree 和运行产物就落在那里，把它算进"不干净"
    会让第二次运行直接中止。
    """
    res = _git(repo, "status", "--porcelain")
    if res.returncode != 0:
        raise RuntimeError(f"不是 git 仓库或 git 不可用：{repo}")
    dirty = [ln for ln in res.stdout.splitlines()
             if ln.strip() and not ln[3:].lstrip('"').startswith(".aifix/")]
    if dirty:
        raise RuntimeError(
            "工作区不干净，请先提交或 stash：\n" + "\n".join(dirty))


class Worktree:
    """在 .aifix/runs/<run_id>/tree 建立独立分支的 worktree，退出时移除。"""

    def __init__(self, repo: Path, run_id: str) -> None:
        self.repo = Path(repo)
        self.run_id = run_id
        self.branch = f"aifix/{run_id}"
        self.root = self.repo / ".aifix" / "runs" / run_id
        self.path = self.root / "tree"

    def __enter__(self) -> "Worktree":
        self.root.mkdir(parents=True, exist_ok=True)
        res = _git(self.repo, "worktree", "add", "-b", self.branch,
                   str(self.path), "HEAD")
        if res.returncode != 0:
            raise RuntimeError(f"创建 worktree 失败：{res.stderr.strip()}")
        return self

    def __exit__(self, *exc: object) -> None:
        # 只移除 worktree 目录，**保留分支**——分支是交付物
        _git(self.repo, "worktree", "remove", "--force", str(self.path))

    def has_changes(self) -> bool:
        return bool(_git(self.path, "diff", "--stat").stdout.strip())

    def diff(self) -> str:
        return _git(self.path, "diff").stdout

    def rollback(self) -> None:
        """丢弃未提交的改动。已 commit 的轮次不受影响。"""
        _git(self.path, "checkout", "--", ".")
        _git(self.path, "clean", "-fd")

    def commit(self, message: str) -> None:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)
```

`Worktree.commit` 依赖 worktree 内可用的 `user.name` / `user.email`；`buggy_repo` 夹具已在仓库级配置，真实使用则依赖用户的全局配置。

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_delivery.py -q
```

预期：7 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/delivery.py tests/test_delivery.py
git commit -m "feat(delivery): worktree 隔离、回滚与提交

退出时只移除 worktree 目录、保留分支 —— 分支就是交付物。"
```

---

### 任务 15：配置

**文件：**
- 创建：`src/aifix/config.py`
- 测试：`tests/test_config.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_config.py`：

```python
from aifix.config import AifixConfig


def test_defaults():
    c = AifixConfig()
    assert c.max_attempts == 3
    assert c.budget_usd == 2.0
    assert c.budget_tokens == 500_000
    assert c.budget_wall_seconds == 1800.0
    assert c.allow_test_edits is False
    assert c.fixer_max_steps == 25


def test_nested_env_overrides(monkeypatch):
    monkeypatch.setenv("AIFIX_DETECTOR__MODEL", "glm-4.6")
    monkeypatch.setenv("AIFIX_FIXER__MODEL", "deepseek-chat")
    c = AifixConfig()
    assert c.detector.model == "glm-4.6"
    assert c.fixer.model == "deepseek-chat"


def test_scalar_env_override(monkeypatch):
    monkeypatch.setenv("AIFIX_MAX_ATTEMPTS", "5")
    assert AifixConfig().max_attempts == 5
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_config.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.config'`。

- [ ] **步骤 3：编写实现**

`src/aifix/config.py`：

```python
from __future__ import annotations

from harness.config import HarnessConfig
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AifixConfig(BaseSettings):
    """两条模型路由 + 预算 + 阈值。

    嵌套环境变量：AIFIX_DETECTOR__MODEL / AIFIX_FIXER__BASE_URL 等。
    """

    model_config = SettingsConfigDict(
        env_prefix="AIFIX_", env_nested_delimiter="__", extra="ignore")

    detector: HarnessConfig = Field(default_factory=HarnessConfig)
    fixer: HarnessConfig = Field(default_factory=HarnessConfig)

    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0

    max_attempts: int = 3
    fixer_max_steps: int = 25
    detector_max_tokens: int = 20_000
    loop_detect_window: int = 3
    tool_result_max_chars: int = 8000

    allow_test_edits: bool = False
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_config.py -q
```

预期：3 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/config.py tests/test_config.py
git commit -m "feat(config): AifixConfig（双模型路由 + 预算 + 阈值）"
```

---

### 任务 16：图状态与 preflight / baseline 节点

**文件：**
- 创建：`src/aifix/graph.py`、`src/aifix/nodes/preflight.py`、`src/aifix/nodes/baseline.py`
- 测试：`tests/test_nodes_preflight_baseline.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_nodes_preflight_baseline.py`：

```python
import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import AifixState, new_state
from aifix.nodes.baseline import baseline_node
from aifix.nodes.preflight import preflight_node


def test_new_state_defaults(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    assert st["run_id"] == "r1"
    assert st["queue"] == []
    assert st["current"] is None
    assert st["attempt"] == 0
    assert st["results"] == []


def test_preflight_detects_adapter_and_rejects_dirty(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["adapter_name"] == "pytest"
    assert out["abort"] is None

    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    st2 = new_state(buggy_repo, AifixConfig(), run_id="r2")
    out2 = preflight_node(st2)
    assert out2["abort"] is not None
    assert "工作区不干净" in out2["abort"]


def test_preflight_rejects_unknown_project(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    st = new_state(tmp_path, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["abort"] is not None
    assert "适配器" in out["abort"]


async def test_baseline_collects_failures(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert "tests/test_calc.py::test_add" in out["baseline_ids"]
    assert "tests/test_calc.py::test_identity" not in out["baseline_ids"]
    assert out["queue"] == ["tests/test_calc.py::test_add"]


async def test_baseline_on_green_repo_yields_empty_queue(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    import subprocess
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert out["queue"] == []
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_nodes_preflight_baseline.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.graph'`。

- [ ] **步骤 3：编写实现**

`src/aifix/graph.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from .config import AifixConfig


class AifixState(TypedDict, total=False):
    """LangGraph 的宏观状态：跨 failure 的进度。

    单次 AgentLoop 内部的微观状态由框架自己管，两层互不知道对方。
    """
    run_id: str
    repo: str
    config: AifixConfig

    adapter_name: str
    worktree_path: str
    branch: str

    baseline_ids: list[str]
    queue: list[str]
    current: str | None
    attempt: int

    diagnosis: dict[str, Any] | None
    verdict: str | None

    spent_usd: float
    spent_tokens: int

    results: list[dict[str, Any]]
    abort: str | None
    report_md: str

    # baseline 解析出的 Failure 对象，按 test_id 索引。
    # 下划线前缀表示它不参与路由判断，只作为 detect / verify 的数据源。
    _failures: dict[str, Any]


def new_state(repo: Path, config: AifixConfig, run_id: str) -> AifixState:
    return AifixState(
        run_id=run_id, repo=str(repo), config=config,
        adapter_name="", worktree_path="", branch="",
        baseline_ids=[], queue=[], current=None, attempt=0,
        diagnosis=None, verdict=None,
        spent_usd=0.0, spent_tokens=0,
        results=[], abort=None,
    )
```

`src/aifix/nodes/preflight.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.pytest_adapter import PytestAdapter
from ..delivery import ensure_clean
from ..graph import AifixState

ADAPTERS = [PytestAdapter]


def preflight_node(state: AifixState) -> dict[str, Any]:
    """探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。"""
    repo = Path(state["repo"])
    for cls in ADAPTERS:
        if cls.detect(repo):
            break
    else:
        return {"abort": f"没有适配器认领这个项目：{repo}"}
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e)}
    return {"adapter_name": cls.name, "abort": None}
```

`src/aifix/nodes/baseline.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.sandbox.local import LocalSandbox

from ..adapters.junit import parse_junit
from ..adapters.pytest_adapter import PytestAdapter
from ..graph import AifixState

_ADAPTERS = {"pytest": PytestAdapter}


def adapter_for(name: str) -> PytestAdapter:
    return _ADAPTERS[name]()


async def run_full_suite(worktree: Path, adapter: PytestAdapter,
                         timeout: float = 900.0):
    """在 worktree 里跑全量测试并解析报告。零 LLM。"""
    report = adapter.report_glob()
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.full_test_command(report), timeout)
        return parse_junit([worktree / report], adapter.make_test_id)
    finally:
        await sb.exec(["rm", "-f", report], 10.0)
        await sb.close()


async def baseline_node(state: AifixState) -> dict[str, Any]:
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。"""
    adapter = adapter_for(state["adapter_name"])
    fs = await run_full_suite(Path(state["worktree_path"]), adapter)
    ids = sorted(fs.ids)
    return {"baseline_ids": ids, "queue": list(ids),
            "_failures": dict(fs.failures)}
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_nodes_preflight_baseline.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/graph.py src/aifix/nodes/preflight.py \
        src/aifix/nodes/baseline.py tests/test_nodes_preflight_baseline.py
git commit -m "feat(nodes): AifixState 与 preflight / baseline 节点"
```

---

### 任务 17：detect / fix / verify 节点

**文件：**
- 创建：`src/aifix/nodes/detect.py`、`src/aifix/nodes/fix.py`、`src/aifix/nodes/verify.py`
- 测试：`tests/test_nodes_loop.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_nodes_loop.py`：

```python
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure, FailureSet, Verdict
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.detect import detect_node
from aifix.nodes.fix import fix_node
from aifix.nodes.preflight import preflight_node
from aifix.nodes.verify import verify_node

_TID = "tests/test_calc.py::test_add"

_DIAG_JSON = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args, cid="c1"):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id=cid, name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _failure_state(buggy_repo, wt):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["baseline_ids"] = [_TID]
    st["queue"] = []
    st["current"] = _TID
    st["attempt"] = 1
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="tests.test_calc", name="test_add",
        message="assert -1 == 5", trace="E assert -1 == 5")}
    return st


async def test_detect_produces_diagnosis(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        out = await detect_node(st, client=_Scripted([_text(_DIAG_JSON)]))
        assert out["diagnosis"]["suspect_file"] == "calc.py"
        assert out["spent_tokens"] == 15


async def test_detect_degrades_on_garbage(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        out = await detect_node(st, client=_Scripted([_text("不是 JSON")]))
        assert out["diagnosis"] is None      # 降级而非中止


async def test_fix_applies_patch(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        st["diagnosis"] = json.loads(_DIAG_JSON)
        client = _Scripted([
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
            _text("已修复"),
        ])
        out = await fix_node(st, client=client)
        assert out["abort"] is None
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_better_commits(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        out = await verify_node(st)
        assert out["verdict"] == Verdict.BETTER.value
        assert out["current"] is None            # 该 failure 完结
        # 已提交：回滚不应还原
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_same_rolls_back(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(
            "def add(a, b):\n    return a - b  # 无用改动\n", encoding="utf-8")
        out = await verify_node(st)
        assert out["verdict"] == Verdict.SAME.value
        assert "无用改动" not in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_gives_up_after_max_attempts(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        st["attempt"] = st["config"].max_attempts
        out = await verify_node(st)
        assert out["current"] is None
        assert out["results"][-1]["abort_reason"] == "max_attempts"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_nodes_loop.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.nodes.detect'`。

- [ ] **步骤 3：编写实现**

`src/aifix/nodes/detect.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient, json_output
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.tools.base import ToolRegistry

from ..agents.detector import SYSTEM_PROMPT, build_prompt, parse_diagnosis
from ..agents.runner import consume
from ..graph import AifixState
from .baseline import adapter_for


async def detect_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """无工具、单步、强制 JSON。解析失败降级为 diagnosis=None。"""
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_for(state["adapter_name"])
    candidates = adapter.locate_source(failure, Path(state["worktree_path"]))

    loop = AgentLoop(
        client=client or OpenAICompatibleClient(cfg.detector),
        registry=ToolRegistry(),                       # 空：模型必然一步出文本
        context=ContextManager(SYSTEM_PROMPT),
        max_steps=1,
        budget=BudgetTracker(max_tokens=cfg.detector_max_tokens),
        model_name=cfg.detector.model,
    )
    with json_output():
        outcome = await consume(loop.run(build_prompt(failure, candidates)))

    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    return {
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
    }
```

`src/aifix/nodes/fix.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.sandbox.local import LocalSandbox

from ..agents.detector import Diagnosis
from ..agents.fixer import SYSTEM_PROMPT, build_initial_messages, build_registry
from ..agents.runner import consume
from ..graph import AifixState
from .baseline import adapter_for


async def fix_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_for(state["adapter_name"])
    raw = state.get("diagnosis")
    diagnosis = Diagnosis.model_validate(raw) if raw else None

    remaining = max(cfg.budget_tokens - state["spent_tokens"], 10_000)
    sandbox = LocalSandbox(workspace=state["worktree_path"])
    await sandbox.start()
    try:
        loop = AgentLoop(
            client=client or OpenAICompatibleClient(cfg.fixer),
            registry=build_registry(sandbox, adapter,
                                    known_ids=set(state["baseline_ids"])),
            context=ContextManager(SYSTEM_PROMPT),
            max_steps=cfg.fixer_max_steps,
            budget=BudgetTracker(max_tokens=remaining,
                                 max_wall_seconds=cfg.budget_wall_seconds),
            loop_detect_window=cfg.loop_detect_window,
            tool_result_max_chars=cfg.tool_result_max_chars,
            model_name=cfg.fixer.model,
        )
        outcome = await consume(
            loop.run(messages=build_initial_messages(failure, diagnosis)))
    finally:
        await sandbox.close()

    return {
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
        "abort": None,      # AgentLoop 的错误不中止整个 run，交给 verify 判定
    }
```

`src/aifix/nodes/verify.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import FailureSet, Verdict
from ..delivery import Worktree
from ..graph import AifixState
from ..verify import compare
from .baseline import adapter_for, run_full_suite


def _worktree(state: AifixState) -> Worktree:
    """指向已存在的 worktree —— **不进入上下文管理器**。

    worktree 由 cli.run_once 建立并负责移除；这里只借用 commit / rollback
    这两个纯路径操作。若在此 `with`，退出时会把还在用的 worktree 删掉。
    """
    return Worktree(Path(state["repo"]), run_id=state["run_id"])


async def verify_node(state: AifixState) -> dict[str, Any]:
    """跑全量、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapter = adapter_for(state["adapter_name"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    current = await run_full_suite(Path(state["worktree_path"]), adapter)
    verdict = compare(baseline, current, target)

    results = list(state["results"])
    if verdict is Verdict.BETTER:
        wt.commit(f"fix: {target}")
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"], "abort_reason": None})
        return {"verdict": verdict.value, "current": None,
                "attempt": 0, "results": results, "diagnosis": None}

    wt.rollback()
    if state["attempt"] >= cfg.max_attempts:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"],
                        "abort_reason": "max_attempts"})
        return {"verdict": verdict.value, "current": None,
                "attempt": 0, "results": results, "diagnosis": None}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None}
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_nodes_loop.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/nodes/detect.py src/aifix/nodes/fix.py \
        src/aifix/nodes/verify.py tests/test_nodes_loop.py
git commit -m "feat(nodes): detect / fix / verify

verify 零 LLM：跑全量、三态判定、BETTER 则提交、否则回滚；
attempt 达上限即放弃该 failure 并继续下一个。"
```

---

### 任务 18：report 节点与图装配

**文件：**
- 创建：`src/aifix/nodes/report.py`；修改：`src/aifix/graph.py`
- 测试：`tests/test_graph.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_graph.py`：

```python
from aifix.graph import route_after_verify, route_after_baseline
from aifix.nodes.report import render_report


def test_route_after_baseline_ends_when_green():
    assert route_after_baseline({"queue": [], "abort": None}) == "report"


def test_route_after_baseline_continues_when_failures():
    assert route_after_baseline({"queue": ["a"], "abort": None}) == "detect"


def test_route_after_baseline_aborts():
    assert route_after_baseline({"queue": ["a"], "abort": "坏了"}) == "report"


def test_route_after_verify_retries_same_failure():
    assert route_after_verify({"current": "a", "queue": [], "abort": None}) == "detect"


def test_route_after_verify_takes_next():
    assert route_after_verify({"current": None, "queue": ["b"], "abort": None}) == "detect"


def test_route_after_verify_reports_when_done():
    assert route_after_verify({"current": None, "queue": [], "abort": None}) == "report"


def test_render_report_lists_outcomes():
    md = render_report({
        "run_id": "r1", "branch": "aifix/r1", "adapter_name": "pytest",
        "baseline_ids": ["a", "b"], "spent_usd": 0.23, "spent_tokens": 12345,
        "abort": None,
        "results": [
            {"test_id": "a", "verdict": "better", "attempts": 1, "abort_reason": None},
            {"test_id": "b", "verdict": "same", "attempts": 3,
             "abort_reason": "max_attempts"},
        ],
    })
    assert "aifix/r1" in md
    assert "1 / 2" in md
    assert "$0.23" in md
    assert "max_attempts" in md


def test_render_report_shows_abort():
    md = render_report({
        "run_id": "r1", "branch": "", "adapter_name": "",
        "baseline_ids": [], "spent_usd": 0.0, "spent_tokens": 0,
        "results": [], "abort": "工作区不干净",
    })
    assert "工作区不干净" in md
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_graph.py -q
```

预期：FAIL，`ImportError: cannot import name 'route_after_verify'`。

- [ ] **步骤 3：编写实现**

`src/aifix/nodes/report.py`：

```python
from __future__ import annotations

from typing import Any

_VERDICT_CN = {"better": "已修复", "same": "未改善", "worse": "引入回归"}


def render_report(state: dict[str, Any]) -> str:
    if state.get("abort"):
        return (f"# aifix run {state['run_id']}\n\n"
                f"**中止**：{state['abort']}\n")

    results = state["results"]
    fixed = sum(1 for r in results if r["verdict"] == "better")
    total = len(state["baseline_ids"])
    lines = [
        f"# aifix run {state['run_id']}",
        "",
        f"- 适配器：{state['adapter_name']}",
        f"- 分支：`{state['branch']}`",
        f"- 修复：**{fixed} / {total}**",
        f"- 成本：${state['spent_usd']:.2f}（{state['spent_tokens']:,} tokens）",
        "",
        "| 测试用例 | 结果 | 尝试次数 | 中止原因 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r['test_id']}` | {_VERDICT_CN.get(r['verdict'], r['verdict'])} "
            f"| {r['attempts']} | {r['abort_reason'] or '—'} |")
    lines += ["", f"合并：`git merge {state['branch']}`"]
    return "\n".join(lines) + "\n"


def report_node(state: dict[str, Any]) -> dict[str, Any]:
    return {"report_md": render_report(state)}
```

追加到 `src/aifix/graph.py` 末尾：

```python
def route_after_baseline(state: AifixState) -> str:
    """全绿或已中止 → 直接出报告；否则取第一个 failure 开始处理。"""
    if state.get("abort") or not state["queue"]:
        return "report"
    return "detect"


def route_after_verify(state: AifixState) -> str:
    """current 仍在 → 同一个 failure 重试；已清空 → 取下一个或收尾。"""
    if state.get("abort"):
        return "report"
    if state["current"] is not None:
        return "detect"
    return "detect" if state["queue"] else "report"


def build_graph(checkpointer: Any = None):
    """装配 LangGraph。节点是 trace 的单位，也是 checkpoint 的边界。"""
    from langgraph.graph import END, StateGraph

    from .nodes.baseline import baseline_node
    from .nodes.detect import detect_node
    from .nodes.fix import fix_node
    from .nodes.preflight import preflight_node
    from .nodes.report import report_node
    from .nodes.verify import verify_node

    def _take_next(state: AifixState) -> dict[str, Any]:
        if state["current"] is not None:
            return {}
        queue = list(state["queue"])
        return {"current": queue.pop(0), "queue": queue, "attempt": 1}

    g = StateGraph(AifixState)
    g.add_node("preflight", preflight_node)
    g.add_node("baseline", baseline_node)
    g.add_node("take_next", _take_next)
    g.add_node("detect", detect_node)
    g.add_node("fix", fix_node)
    g.add_node("verify", verify_node)
    g.add_node("report", report_node)

    g.set_entry_point("preflight")
    g.add_conditional_edges(
        "preflight",
        lambda s: "report" if s.get("abort") else "baseline",
        {"report": "report", "baseline": "baseline"})
    g.add_conditional_edges(
        "baseline", route_after_baseline,
        {"report": "report", "detect": "take_next"})
    g.add_edge("take_next", "detect")
    g.add_edge("detect", "fix")
    g.add_edge("fix", "verify")
    g.add_conditional_edges(
        "verify", route_after_verify,
        {"report": "report", "detect": "take_next"})
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
```

`graph.py` 顶部已导入 `Any`，无需新增。

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_graph.py -q
```

预期：8 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/graph.py src/aifix/nodes/report.py tests/test_graph.py
git commit -m "feat(graph): 路由函数、report 节点与 LangGraph 装配"
```

---

### 任务 19：CLI 与端到端验收

**文件：**
- 创建：`src/aifix/cli.py`
- 测试：`tests/test_e2e.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_e2e.py`：

```python
"""端到端验收：红色测试进去，绿色分支出来，主工作区未被触碰。

用脚本化模型替身，不打网络。
"""
import json
import subprocess

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


async def test_red_in_green_out(buggy_repo):
    detector = _Scripted([_text(_DIAG)])
    fixer = _Scripted([
        _tool("apply_patch", json.dumps({"diff": _PATCH})),
        _text("已修复"),
    ])
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")

    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e",
                           detector_client=detector, fixer_client=fixer)

    # 1. 目标用例被判定为修复
    assert [r["verdict"] for r in state["results"]] == ["better"]
    # 2. 主工作区一字未动
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original
    # 3. 修复落在独立分支上
    show = subprocess.run(["git", "show", "aifix/e2e:calc.py"],
                          cwd=buggy_repo, capture_output=True, text=True)
    assert "a + b" in show.stdout
    # 4. 报告可读
    assert "1 / 1" in state["report_md"]


async def test_green_repo_reports_no_work(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e2",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["results"] == []
    assert "0 / 0" in state["report_md"]


async def test_dirty_repo_aborts(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e3",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["abort"] is not None
    assert "工作区不干净" in state["report_md"]
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_e2e.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.cli'`。

- [ ] **步骤 3：编写实现**

`src/aifix/cli.py`：

```python
from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path
from typing import Any

from .config import AifixConfig
from .delivery import Worktree
from .graph import AifixState, new_state
from .nodes.baseline import baseline_node
from .nodes.detect import detect_node
from .nodes.fix import fix_node
from .nodes.preflight import preflight_node
from .nodes.report import render_report
from .nodes.verify import verify_node


async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> AifixState:
    """按状态图的语义顺序执行一次完整 run。

    M1 直接手工驱动节点，语义与 build_graph() 的图完全一致——把
    LangGraph 的 checkpointer 接进来是 M2 的事（需要先有 trace 落盘）。
    """
    state = new_state(repo, config, run_id=run_id)
    state.update(preflight_node(state))
    if state["abort"]:
        state["report_md"] = render_report(state)
        return state

    with Worktree(repo, run_id=run_id) as wt:
        state["worktree_path"] = str(wt.path)
        state["branch"] = wt.branch

        # 全量测试很贵，整个 run 只在这里跑一次；后续每轮 verify 各跑一次
        state.update(await baseline_node(state))

        while True:
            if state["current"] is None:
                if state["abort"] or not state["queue"]:
                    break
                state["current"] = state["queue"].pop(0)
                state["attempt"] = 1
            state.update(await detect_node(state, client=detector_client))
            state.update(await fix_node(state, client=fixer_client))
            state.update(await verify_node(state))

    state["report_md"] = render_report(state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(prog="aifix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="修复当前 repo 的失败测试")
    run.add_argument("repo", nargs="?", default=".")
    args = parser.parse_args()

    if args.cmd == "run":
        state = asyncio.run(run_once(
            Path(args.repo).resolve(), AifixConfig(), run_id=uuid.uuid4().hex[:8]))
        print(state["report_md"])
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_e2e.py -q
```

预期：3 passed。

- [ ] **步骤 5：运行全量测试**

```bash
uv run pytest -q
```

预期：全部 PASS（约 75 项）。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/cli.py tests/test_e2e.py
git commit -m "feat(cli): aifix run 与端到端验收

红色测试进去、绿色分支出来、主工作区未被触碰 —— 三条断言全部覆盖。"
git push origin main
```

---

## M1 完成标志

- [ ] `uv run pytest -q` 全绿
- [ ] `../ai-harness-framework` 全绿（663 passed）
- [ ] `../ai-learning-helper` 全绿（1906 passed）——框架改动未破坏原消费者
- [ ] `aifix run` 在一个真实的第三方 pytest 项目上跑通一次（用真实模型端点，非替身）

最后一条需要手动执行：

```bash
export AIFIX_FIXER__API_KEY=... AIFIX_FIXER__BASE_URL=https://api.deepseek.com/v1 \
       AIFIX_FIXER__MODEL=deepseek-chat
export AIFIX_DETECTOR__API_KEY=$AIFIX_FIXER__API_KEY \
       AIFIX_DETECTOR__BASE_URL=$AIFIX_FIXER__BASE_URL \
       AIFIX_DETECTOR__MODEL=deepseek-chat
cd /path/to/some/pytest/project && aifix run
```

## 交给 M2 的已知缺口

M1 刻意不做，避免范围蔓延：

| 缺口 | 归属 |
|---|---|
| 空 diff 守卫（模型宣称修好但没改） | M2 |
| 巨型 diff 守卫（整文件重写） | M2 |
| flaky 过滤（新失败重跑确认） | M2 |
| 连续失败熔断 | M2 |
| 三层预算动态分配（M1 只做粗略扣减） | M2 |
| 三层嵌套 trace、`events.jsonl`、SQLite 轨迹 | M2 |
| LangGraph `SqliteSaver` 接入与断点续跑（`build_graph()` 已就位但 `run_once` 暂未使用） | M2 |
