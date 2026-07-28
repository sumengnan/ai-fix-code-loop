# M3 可度量：任务集与评测 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让「这套系统到底行不行、换个模型行不行」变成一张有证据的表，而不是感觉。

**架构：** 从目标仓库的 git history 挖出自带 ground truth 的任务集（`aifix mine`），把每个任务还原成一个独立克隆，并行跑完整的 M1/M2 闭环（`aifix eval`），按规格 §9 的双档打分汇总成跨模型对比表。判定复用同一套 `compare()` —— 评测不另写一份，否则测的是另一个系统。

**技术栈：** Python ≥3.11 · `ai-harness-framework` 0.0.2 · pydantic v2 · asyncio · pytest

**规格：** `docs/superpowers/specs/2026-07-27-ai-fix-code-loop-design.md`（§9 评测、§13 M3）
**前置：** M2 已完成并合并（`main` @ 163 passed）

## 范围拆分

规格 §13 给 M3 列了四件事：`MavenAdapter`、`aifix mine`、`aifix eval`、`aifix replay`，验收是「跑出第一张跨模型对比表」。

**本计划只做任务集与评测**（`mine` + `eval` + 打分 + 对比表）—— 它单独就能满足 M3 的验收，且是另外两件事的前提：没有评测集，`MavenAdapter` 写完了也无从证明适配层抽象成立。

`MavenAdapter` 与 `aifix replay` 另起一份计划（M3b）。理由见「交给 M3b 的缺口」。

## 决策记录

### 1. `locate_hit` 的定义冲突（必须先解决）

规格自身矛盾：

| 位置 | 定义 |
|---|---|
| §8 表格（第 336 行） | `suspect_file` 是否命中**候选**——即 `locate_source` 从 traceback 抽出的文件 |
| §9 `TaskResult`（第 388 行） | `suspect_file` ∈ **ground truth 改动文件** |

M2 实现的是 §8 那个。两者是不同的集合：traceback 指向的文件未必是该改的文件（典型的例子是异常在下游被抛出，缺陷在上游）。评测若按 §8 注释里那句「§9 直接取用」去取，量出来的**不是定位准确率**，而是「模型有没有照抄 traceback」。

**决策：** `locate_hit` 保留 §9 的语义（对 ground truth），由评测计算；M2 那个改名为 `suspect_in_traceback`。两个都有价值——后者衡量的是「模型是否只会照抄 traceback」，恰好是前者的对照组——但绝不能共用一个名字。

### 2. 一个任务一个目标用例

规格 §9 的 `TaskResult` 是单一 `verdict` / 单一 `attempts`。一个 commit 修好多个测试时，产出多个任务而不是一个多目标任务。代价是同一个 commit 会被跑多次；收益是 `TaskResult` 不必引入「部分成功」这种没人想解释的状态。

### 3. 克隆而非 worktree

每个任务独占一个 `git clone --local` 的完整仓库，不用 worktree。worktree 共享同一个 `.git`：并行跑几十个任务时分支名、index、gc 会互相踩，而 aifix 自己**还要在任务仓库里再开一个 worktree**。`--local` 克隆走硬链接，几乎不额外占盘。

### 4. 评测直接 `await run_once`，不起子进程

同进程调用意味着评测跑的就是产品代码本身，配置、trace、判定全都是同一套。子进程能拿到更强的崩溃隔离，但代价是配置要经环境变量绕一圈——而配置正是跨模型对比要动的东西。崩溃隔离改用「每个任务包一层 try，失败记成带 `error` 的 `TaskResult`」来解决。

### 5. 与规格的偏差（自检发现，已确认）

**（1）不做 C 类冒烟集生成器**

规格 §9「并行与 CI」要求「改完 prompt / 换完模型 / 动完工具边界后跑**冒烟集（C 类，5~10 个）**」，C 类指人造变异生成的任务。**本计划不做 C 类生成器**。

理由：`mine` 产出的 A 类任务同样可以当冒烟集用（`--max-tasks 5`），而且分布真实。C 类的唯一优势是「便宜可批量」——在还没有任何评测数字的时候，先花力气造一批分布已知失真的任务，是拿确定的偏差换不确定的速度。等真跑过一轮、知道 A 类的耗时到底是不是瓶颈了，再决定要不要补 C 类。

记入「交给 M3b 的缺口」。

**（2）对比表出的是「平均尝试」而非规格的「平均步数」**

规格 §9 的跨模型对比表列的是**平均步数**——agent 在一次 AgentLoop 里走了多少步，受 `fixer_max_steps=25` 约束，量的是「模型解一道题要绕多少圈」。实现出的是**平均尝试**（`Summary.avg_attempts`）——修复轮数，受 `max_attempts=3` 约束，取值只在 1~3。这是两个不同的量：一次尝试里可能走 1 步，也可能走满 25 步。

可以接受的理由：平均尝试同样有信息量（「一次就修好」与「改三轮才修好」是模型能力的真实差别），且 `attempts` 已经在 `TaskResult` 里、零成本；而平均步数要额外从 `events.jsonl` 里统计 `StepStarted`/`ModelCall` 一类事件，是另一条数据通路。在还没有任何一轮真实评测数字之前，先把便宜的那个量拿到手更划算。

代价要说清楚：平均尝试的分辨率只有 3 档，区分不出「一次尝试里绕了 3 步」和「绕了 24 步」——而后者恰恰是 token 成本的主要来源。等第一轮跨模型对比跑完、看清成本差异到底出在哪里，再决定是补上平均步数还是两个都留。

留给**下一个里程碑**。

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `src/aifix/eval/__init__.py` | 包标记 |
| `src/aifix/eval/task.py` | `Task` / `TaskResult` 模型与 jsonl 读写 |
| `src/aifix/eval/workspace.py` | 把一个任务还原成可直接交给 aifix 的仓库 |
| `src/aifix/eval/mine.py` | 从 git history 挖任务集 |
| `src/aifix/eval/runner.py` | 单任务执行与并行调度 |
| `src/aifix/eval/score.py` | 双档打分与跨模型对比表 |
| `src/aifix/violations.py` | 从事件流数出越界尝试（纯函数） |

**修改**

| 文件 | 变更 |
|---|---|
| `src/aifix/nodes/detect.py` | `locate_hit` → `suspect_in_traceback` |
| `src/aifix/nodes/fix.py` | 记 `violation` 事实 |
| `src/aifix/cli.py` | `mine` / `eval` / `eval-report` 三个子命令 |
| `tests/conftest.py` | 新增 `history_repo` 夹具（带一个红转绿的 commit） |

**框架侧：无改动。**

---

# 阶段 1：数据与还原

### 任务 1：`Task` / `TaskResult` 与 jsonl 读写

**文件：**
- 创建：`src/aifix/eval/__init__.py`、`src/aifix/eval/task.py`
- 测试：`tests/test_eval_task.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_task.py`：

```python
import pytest

from aifix.eval.task import Task, TaskResult, read_jsonl, write_jsonl

_T = Task(task_id="proj@abc1234::tests/test_calc.py::test_add",
          repo="/tmp/proj", commit="abc1234", base_commit="def5678",
          test_files=["tests/test_calc.py"],
          target_test="tests/test_calc.py::test_add",
          gold_files=["calc.py"])


def test_roundtrip(tmp_path):
    p = tmp_path / "tasks.jsonl"
    write_jsonl(p, [_T])
    back = read_jsonl(p, Task)
    assert back == [_T]


def test_creates_parent_directory(tmp_path):
    p = tmp_path / "deep" / "nested" / "tasks.jsonl"
    write_jsonl(p, [_T])
    assert p.is_file()


def test_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text(_T.model_dump_json() + "\n\n\n", encoding="utf-8")
    assert len(read_jsonl(p, Task)) == 1


def test_non_ascii_survives_roundtrip(tmp_path):
    """中文路径不能被转义成 \\uXXXX —— 任务集是要人看的。"""
    t = _T.model_copy(update={"gold_files": ["源码/计算.py"]})
    p = tmp_path / "tasks.jsonl"
    write_jsonl(p, [t])
    assert "源码/计算.py" in p.read_text(encoding="utf-8")
    assert read_jsonl(p, Task)[0].gold_files == ["源码/计算.py"]


def test_result_defaults():
    r = TaskResult(task_id="x", model="m", locate_hit=False, suspect_file=None,
                   verdict="same", attempts=1, tokens=10, cost_usd=0.1,
                   violations=0)
    assert r.abort_reason is None
    assert r.error is None


def test_adapter_defaults_to_pytest():
    assert _T.adapter == "pytest"


def test_missing_required_field_rejected():
    with pytest.raises(Exception):
        Task(task_id="x")
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_task.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.eval'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/eval/__init__.py`（空文件）。

创建 `src/aifix/eval/task.py`：

```python
"""评测任务与结果的数据模型。

任务集是一份 jsonl：一行一个任务，自带 ground truth —— gold_files 来自
那个把测试从红修到绿的 commit，不需要人来标注。

一个 target_test 一个任务（而不是一个 commit 一个任务），是为了让
TaskResult 保持规格 §9 的形状：单一 verdict、单一 attempts。一个 commit
修好多个测试时产出多个任务。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel


class Task(BaseModel):
    task_id: str
    repo: str                    # 源仓库绝对路径，评测从这里克隆
    commit: str                  # 把测试从红修到绿的那个 commit
    base_commit: str             # 它的父提交：源码要回到这里
    test_files: list[str]        # 从 commit 取来覆盖上去的测试文件
    target_test: str             # 期望被修绿的那一个用例
    gold_files: list[str]        # commit 里改动的源码文件 —— ground truth
    adapter: str = "pytest"


class TaskResult(BaseModel):
    task_id: str
    model: str
    locate_hit: bool             # suspect_file ∈ gold_files（规格 §9 的定义）
    suspect_file: str | None
    verdict: str
    attempts: int
    tokens: int
    cost_usd: float
    violations: int
    abort_reason: str | None = None
    # 任务本身跑挂了（克隆失败、baseline 没复现……）。与「没修好」是两回事：
    # 前者是评测的问题，后者是被测系统的成绩，混在一起会污染成功率。
    error: str | None = None


M = TypeVar("M", bound=BaseModel)


def write_jsonl(path: Path, items: Iterable[BaseModel]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(it.model_dump_json() + "\n")


def read_jsonl(path: Path, model: type[M]) -> list[M]:
    out: list[M] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(model.model_validate_json(line))
    return out
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_task.py -q
```

预期：7 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval tests/test_eval_task.py
git commit -m "feat(eval): Task / TaskResult 模型与 jsonl 读写

一个 target_test 一个任务，让 TaskResult 保持规格 §9 的形状：
单一 verdict、单一 attempts，不必引入「部分成功」这种没人想解释的状态。

error 与「没修好」严格分开 —— 前者是评测自己的问题，
后者是被测系统的成绩，混在一起会污染成功率。"
```

---

### 任务 2：`history_repo` 夹具

后面三个任务都要一个「带红转绿 commit」的仓库，先把它建好。

**文件：**
- 修改：`tests/conftest.py`
- 测试：`tests/test_eval_workspace.py`（任务 3 一并验证）

- [ ] **步骤 1：编写夹具**

追加到 `tests/conftest.py` 末尾：

```python
_TEST_ONLY_IDENTITY = '''from calc import add


def test_identity():
    assert add(0, 0) == 0
'''

_TEST_BOTH = '''from calc import add


def test_identity():
    assert add(0, 0) == 0


def test_add():
    assert add(2, 3) == 5
'''


@pytest.fixture
def history_repo(tmp_path: Path) -> dict:
    """一个带「红转绿」commit 的仓库，供挖掘与评测使用。

    C^ : calc.py 有 bug，测试里只有 test_identity（通过）
    C  : calc.py 修好，测试里多出 test_add

    于是「C^ 的源码 + C 的测试」= test_add 红，而 C 处全绿 ——
    正是 aifix mine 要找的形状。
    """
    repo = tmp_path / "hist"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        _TEST_ONLY_IDENTITY, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "calc.py").write_text(_FIXED, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST_BOTH, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: add 应为加法")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    return {"path": repo, "base": base, "commit": commit,
            "test_files": ["tests/test_calc.py"], "gold_files": ["calc.py"],
            "target": "tests/test_calc.py::test_add"}
```

- [ ] **步骤 2：Commit**

```bash
git add tests/conftest.py
git commit -m "test: history_repo 夹具 —— 一个带红转绿 commit 的仓库"
```

---

### 任务 3：把任务还原成可直接交给 aifix 的仓库

**文件：**
- 创建：`src/aifix/eval/workspace.py`
- 测试：`tests/test_eval_workspace.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_workspace.py`：

```python
import subprocess

from aifix.eval.task import Task
from aifix.eval.workspace import materialize, prepare_task_repo


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _task(h) -> Task:
    return Task(task_id="t1", repo=str(h["path"]), commit=h["commit"],
                base_commit=h["base"], test_files=h["test_files"],
                target_test=h["target"], gold_files=h["gold_files"])


def test_source_is_at_base_but_tests_are_from_commit(history_repo, tmp_path):
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert "a - b" in (dest / "calc.py").read_text(encoding="utf-8")
    assert "test_add" in (dest / "tests" / "test_calc.py").read_text(
        encoding="utf-8")


def test_workspace_is_clean(history_repo, tmp_path):
    """必须干净：aifix 的 preflight 会拒绝不干净的仓库。"""
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert _git(dest, "status", "--porcelain").strip() == ""


def test_tests_are_committed_so_worktree_carries_them(history_repo, tmp_path):
    """worktree 从 HEAD 创建 —— 测试不提交的话根本进不去 agent 的工作区。"""
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    tracked = _git(dest, "show", "HEAD:tests/test_calc.py")
    assert "test_add" in tracked


def test_source_repo_untouched(history_repo, tmp_path):
    before = _git(history_repo["path"], "rev-parse", "HEAD").strip()
    prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert _git(history_repo["path"], "rev-parse", "HEAD").strip() == before
    assert _git(history_repo["path"], "status", "--porcelain").strip() == ""


def test_two_workspaces_are_independent(history_repo, tmp_path):
    """并行评测的前提：两个任务工作区互不影响。"""
    a = prepare_task_repo(_task(history_repo), tmp_path / "a")
    b = prepare_task_repo(_task(history_repo), tmp_path / "b")
    (a / "calc.py").write_text("# 改坏 a\n", encoding="utf-8")
    assert "a - b" in (b / "calc.py").read_text(encoding="utf-8")


def test_materialize_is_idempotent_on_unchanged_tests(history_repo, tmp_path):
    """测试文件在 C^ 与 C 完全一致时不该因「没东西可提交」而炸。"""
    h = history_repo
    dest = materialize(str(h["path"]), h["base"], h["base"],
                       h["test_files"], tmp_path / "w")
    assert (dest / "calc.py").is_file()
    assert _git(dest, "status", "--porcelain").strip() == ""
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_workspace.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.eval.workspace'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/eval/workspace.py`：

```python
"""把一个任务还原成一个可以直接交给 aifix 的仓库。

克隆而不是 worktree：worktree 共享同一个 .git，并行跑几十个任务时
分支名、index、gc 都会互相踩 —— 而 aifix 自己还要在任务仓库里再开一个
worktree。`--local` 克隆走硬链接，几乎不额外占盘。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .task import Task


def _git(cwd: Path | None, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{cwd}）：{res.stderr.strip()}")
    return res.stdout


def materialize(repo: str, base_commit: str, commit: str,
                test_files: list[str], dest: Path) -> Path:
    """dest 处得到：源码停在 base_commit，测试来自 commit，且工作区干净。

    干净是硬要求 —— aifix 的 preflight 会拒绝不干净的仓库；而 worktree
    是从 HEAD 创建的，测试不提交的话根本进不到 agent 的工作区。
    """
    dest = Path(dest)
    _git(None, "clone", "--local", "--quiet", "--no-checkout", repo, str(dest))
    _git(dest, "checkout", "--quiet", base_commit)
    if test_files:
        _git(dest, "checkout", commit, "--", *test_files)
    _git(dest, "config", "user.email", "eval@aifix.local")
    _git(dest, "config", "user.name", "aifix-eval")
    if test_files:
        _git(dest, "add", "--", *test_files)
    # 测试文件在 base 与 commit 完全一致时无事可提交，git commit 会以 1 退出
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest)
    if staged.returncode != 0:
        _git(dest, "commit", "--quiet", "-m",
             f"eval: 取 {commit[:8]} 的测试，源码停在 {base_commit[:8]}")
    return dest


def prepare_task_repo(task: Task, dest: Path) -> Path:
    return materialize(task.repo, task.base_commit, task.commit,
                       task.test_files, dest)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_workspace.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval/workspace.py tests/test_eval_workspace.py
git commit -m "feat(eval): 把任务还原成可直接交给 aifix 的仓库

源码回到 C^、测试取自 C，并提交 —— worktree 从 HEAD 创建，
测试不提交的话根本进不去 agent 的工作区。

克隆而非 worktree：并行时 worktree 共享同一个 .git 会互相踩，
而 aifix 自己还要在任务仓库里再开一个 worktree。"
```

---

# 阶段 2：越界统计与指标正名

### 任务 4：从事件流数出越界尝试

规格 §9 对比表的最后一列量化的是「不同模型有多不听话」——正是 harness 存在的理由。

**文件：**
- 创建：`src/aifix/violations.py`
- 测试：`tests/test_violations.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_violations.py`：

```python
from harness.events import RunError, TextDelta, ToolFinished, ToolStarted
from harness.types import ToolCall, ToolResult

from aifix.violations import count_violations


def _started(call_id, name):
    return ToolStarted(tool_call=ToolCall(id=call_id, name=name, arguments={}))


def _finished(call_id, content, is_error=True):
    return ToolFinished(result=ToolResult(call_id, content, is_error=is_error))


def test_counts_test_file_edit_attempt():
    evs = [_started("1", "apply_patch"),
           _finished("1", "拒绝修改测试文件：tests/test_x.py。请修改源码……")]
    assert count_violations(evs)["test_edit"] == 1


def test_counts_path_escape_attempt():
    evs = [_started("1", "apply_patch"),
           _finished("1", "路径逃逸工作区：../../evil.py")]
    assert count_violations(evs)["path_escape"] == 1


def test_counts_loop_abort():
    evs = [RunError(error="检测到疑似循环：纠偏后仍连续重复相同的工具调用"
                          "（apply_patch），已中止")]
    assert count_violations(evs)["loop_abort"] == 1


def test_ordinary_patch_failure_is_not_a_violation():
    """补丁打不上是模型能力问题，不是越界 —— 混进来会让这列失去意义。"""
    evs = [_started("1", "apply_patch"),
           _finished("1", "补丁无法应用（git apply --check 失败）：……")]
    assert count_violations(evs) == {"test_edit": 0, "path_escape": 0,
                                     "loop_abort": 0}


def test_errors_from_other_tools_are_ignored():
    """只有 apply_patch 能越界改文件；别的工具报错不算。"""
    evs = [_started("1", "read_file"),
           _finished("1", "路径逃逸工作区：../../etc/passwd")]
    assert count_violations(evs)["path_escape"] == 0


def test_successful_calls_are_not_counted():
    evs = [_started("1", "apply_patch"),
           _finished("1", "补丁已应用。", is_error=False)]
    assert sum(count_violations(evs).values()) == 0


def test_non_loop_run_error_is_ignored():
    evs = [RunError(error="模型调用失败: 连接超时")]
    assert count_violations(evs)["loop_abort"] == 0


def test_counts_accumulate():
    evs = [_started("1", "apply_patch"),
           _finished("1", "拒绝修改测试文件：tests/a.py"),
           TextDelta(text="换一个"),
           _started("2", "apply_patch"),
           _finished("2", "拒绝修改测试文件：tests/b.py")]
    assert count_violations(evs)["test_edit"] == 2


def test_empty_stream():
    assert count_violations([]) == {"test_edit": 0, "path_escape": 0,
                                    "loop_abort": 0}
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_violations.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.violations'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/violations.py`：

```python
"""从事件流里数出「越界尝试」。

规格 §9 对比表的最后一列量化的是「不同模型有多不听话」—— 正是 harness
存在的理由。三类：想改测试文件、想逃出工作区、原地打转被中止。

刻意不把「补丁打不上」算进来：那是模型能力问题而非越界，混进来这一列
就失去意义了。
"""
from __future__ import annotations

from typing import Any

from harness.events import RunError, ToolFinished, ToolStarted

# 这三条串都由我们自己产生（patch.py / sandbox.base / agent_loop.py），
# 不是对第三方输出的猜测。
_TEST_EDIT = "拒绝修改测试文件"
_PATH_ESCAPE = "路径逃逸"
_LOOP = "检测到疑似循环"

_KINDS = ("test_edit", "path_escape", "loop_abort")


def count_violations(events: list[Any]) -> dict[str, int]:
    """返回 {test_edit, path_escape, loop_abort} 三类计数。"""
    out = dict.fromkeys(_KINDS, 0)
    # 只有 apply_patch 能越界改文件，所以按 tool_call_id 关联回工具名，
    # 而不是假定事件顺序。
    names: dict[str, str] = {}
    for ev in events:
        if isinstance(ev, ToolStarted):
            names[ev.tool_call.id] = ev.tool_call.name
        elif isinstance(ev, ToolFinished) and ev.result.is_error:
            if names.get(ev.result.tool_call_id) != "apply_patch":
                continue
            content = ev.result.content
            if _TEST_EDIT in content:
                out["test_edit"] += 1
            elif _PATH_ESCAPE in content:
                out["path_escape"] += 1
        elif isinstance(ev, RunError) and _LOOP in (ev.error or ""):
            out["loop_abort"] += 1
    return out
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_violations.py -q
```

预期：9 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/violations.py tests/test_violations.py
git commit -m "feat(violations): 从事件流数出越界尝试

想改测试文件 / 想逃出工作区 / 原地打转被中止。刻意不把「补丁打不上」
算进来 —— 那是能力问题不是越界，混进来这一列就失去意义了。

按 tool_call_id 关联回工具名而非假定事件顺序：只有 apply_patch
能越界改文件。"
```

---

### 任务 5：`fix_node` 记 `violation` 事实

**文件：**
- 修改：`src/aifix/nodes/fix.py`
- 测试：`tests/test_trace_wiring.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_trace_wiring.py` 末尾：

```python
async def test_fix_records_violations(buggy_repo, tmp_path):
    """模型想改测试文件 —— 被工具挡下，且必须留下可统计的痕迹。"""
    from harness.llm.base import ToolCallDelta

    from aifix.nodes.fix import fix_node

    bad = """--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -3,4 +3,4 @@
 def test_add():
-    assert add(2, 3) == 5
+    assert True
"""
    good = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

    def _tool(name, args):
        return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=args)),
                StreamChunk(type="done", usage=Usage(10, 5, 15))]

    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        st["baseline_ids"] = [_TID]
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": bad})),
                            _tool("apply_patch", json.dumps({"diff": good})),
                            _text("改源码了")])
        await fix_node(st, client=client)
        trace.close()

    facts = _facts(tmp_path)
    viol = [f for f in facts if f["key"] == "violation"]
    assert [v["value"] for v in viol] == ["test_edit"]


async def test_fix_records_no_violation_when_clean(buggy_repo, tmp_path):
    from harness.llm.base import ToolCallDelta

    from aifix.nodes.fix import fix_node

    good = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

    def _tool(name, args):
        return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=args)),
                StreamChunk(type="done", usage=Usage(10, 5, 15))]

    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        st["baseline_ids"] = [_TID]
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": good})),
                            _text("已修复")])
        await fix_node(st, client=client)
        trace.close()

    assert [f for f in _facts(tmp_path) if f["key"] == "violation"] == []
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_trace_wiring.py -q -k violation
```

预期：`test_fix_records_violations` FAIL（`facts.jsonl` 里没有 `violation`），另一个 PASS。

- [ ] **步骤 3：编写实现**

在 `src/aifix/nodes/fix.py` 顶部导入中加入：

```python
from ..violations import count_violations
```

把循环内 `trace.record_events(outcome.events)` 那一段改为：

```python
            # 每一轮都记：守卫重试时，模型「一字未改」的那一轮恰恰
            # 是最该复盘的，只记最后一轮等于把它丢了。
            trace.record_events(outcome.events)
            for kind, n in count_violations(outcome.events).items():
                for _ in range(n):
                    trace.fact("violation", kind)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_trace_wiring.py -q
```

预期：9 passed。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/nodes/fix.py tests/test_trace_wiring.py
git commit -m "feat(trace): fix 节点记录越界尝试

一条 violation 一行，而不是记一个总数 —— 评测要按类型分组，
汇总成一个数字之后就再也拆不开了。"
```

---

### 任务 6：消解 `locate_hit` 的定义冲突

**文件：**
- 修改：`src/aifix/nodes/detect.py`
- 测试：`tests/test_trace_wiring.py`

- [ ] **步骤 1：修改测试到新名字**

`tests/test_trace_wiring.py` 里 `locate_hit` 共出现 4 次：1 个函数名（第 54 行）、2 处断言（第 63、74 行）、1 处 docstring（第 79 行）。全部改为 `suspect_in_traceback`，并把 `test_detect_records_locate_hit` / `test_detect_records_miss` 的函数名改为 `test_detect_records_suspect_in_traceback` / `test_detect_records_traceback_miss`。在第一个测试的 docstring 里写明：

```python
    """suspect_file 是否落在 traceback 指出的文件里。

    注意这**不是**规格 §9 的 locate_hit（那个对 ground truth 判定，
    由评测计算）。两者是不同的集合：traceback 指向的文件未必是该改的
    文件 —— 异常常在下游抛出，缺陷却在上游。共用一个名字会让评测
    悄悄量错东西，所以在这里就分开。
    """
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_trace_wiring.py -q -k "traceback"
```

预期：FAIL，`facts.jsonl` 里只有 `locate_hit` 没有 `suspect_in_traceback`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/nodes/detect.py` 的对应片段改为：

```python
    if diagnosis is not None:
        # 模型点名的文件是否落在 traceback 指出的候选里。
        # 这不是规格 §9 的 locate_hit —— 那个对 ground truth 判定，由评测
        # 计算。两者是不同的集合：异常常在下游抛出而缺陷在上游。共用一个
        # 名字会让评测悄悄量成「模型有没有照抄 traceback」。
        hit = any(c.path == diagnosis.suspect_file for c in candidates)
        trace.fact("suspect_in_traceback", hit)
        trace.fact("suspect_file", diagnosis.suspect_file)
    else:
        trace.fact("suspect_in_traceback", False)
        trace.fact("diagnosis_parse_failed", True)
```

- [ ] **步骤 4：更新 `tests/test_artifacts.py` 的断言**

把 `test_facts_contain_verdict_and_locate_hit` 里的

```python
    assert "locate_hit" in keys
```

改为

```python
    assert "suspect_in_traceback" in keys
```

并把该测试函数改名为 `test_facts_contain_verdict_and_suspect`。

- [ ] **步骤 5：同步规格**

把规格 `docs/superpowers/specs/2026-07-27-ai-fix-code-loop-design.md` §8 表格里的

```
| `aifix.locate_hit` | detect | `suspect_file` 是否命中候选——§9 直接取用 |
```

改为

```
| `aifix.suspect_in_traceback` | detect | `suspect_file` 是否落在 traceback 候选里。**不是** §9 的 `locate_hit`（那个对 ground truth 判定，由评测计算）——两者是不同的集合 |
| `aifix.violation` | fix | 越界尝试，一条一行：`test_edit` / `path_escape` / `loop_abort` |
```

- [ ] **步骤 6：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/nodes/detect.py tests/test_trace_wiring.py \
        tests/test_artifacts.py docs/superpowers/specs
git commit -m "fix(trace): locate_hit 改名 suspect_in_traceback

规格自身矛盾：§8 说它是「是否命中 traceback 候选」，§9 说它是
「是否 ∈ ground truth 改动文件」，而 §8 还写着「§9 直接取用」。
两者是不同的集合 —— 异常常在下游抛出而缺陷在上游。

按 §8 的实现直接拿去当 §9 的定位准确率，量出来的是「模型有没有
照抄 traceback」。locate_hit 归还给 §9 由评测计算，这个改名。
两个指标都有价值 —— 后者恰是前者的对照组 —— 但不能共用名字。"
```

---

# 阶段 3：挖掘

### 任务 7：候选筛选

**文件：**
- 创建：`src/aifix/eval/mine.py`
- 测试：`tests/test_eval_mine.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_mine.py`：

```python
from aifix.eval.mine import is_candidate, split_paths

_DIRS = ["tests", "test"]


def test_splits_tests_from_source():
    tests, src = split_paths(["tests/test_calc.py", "calc.py"], _DIRS)
    assert tests == ["tests/test_calc.py"]
    assert src == ["calc.py"]


def test_test_prefixed_file_outside_test_dir_counts_as_test():
    """有的项目把测试和源码放一起 —— 按目录判会漏。"""
    tests, src = split_paths(["pkg/test_util.py", "pkg/util.py"], _DIRS)
    assert tests == ["pkg/test_util.py"]
    assert src == ["pkg/util.py"]


def test_non_python_files_are_dropped():
    tests, src = split_paths(["README.md", "calc.py", "data.json"], _DIRS)
    assert tests == []
    assert src == ["calc.py"]


def test_candidate_needs_both_sides():
    assert is_candidate(["tests/t.py"], ["a.py"]) is True
    assert is_candidate([], ["a.py"]) is False       # 没动测试 → 没有 oracle
    assert is_candidate(["tests/t.py"], []) is False  # 没动源码 → 没有 gold


def test_empty_commit_is_not_a_candidate():
    assert is_candidate(*split_paths([], _DIRS)) is False
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_mine.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.eval.mine'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/eval/mine.py`：

```python
"""从 git history 挖任务集。

规格 §9 的做法：
    找出让测试从红变绿的 commit C
    任务 = checkout 到 C^，但保留 C 中的测试文件
    期望 = agent 的补丁让该测试转绿且不引入回归
    对照 = C 中的源码改动即标准答案

自带 ground truth，分布真实 —— 不需要人来标注，也不会像人造变异那样
在分布上跑偏。
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath


def split_paths(paths: list[str],
                test_dirs: list[str]) -> tuple[list[str], list[str]]:
    """把 commit 改动的路径拆成（测试文件, 源文件）。"""
    tests: list[str] = []
    src: list[str] = []
    for p in paths:
        pp = PurePosixPath(p)
        if pp.suffix != ".py":
            continue
        # 目录判 + 文件名判：有的项目把测试和源码放在一起
        if (pp.parts and pp.parts[0] in test_dirs) or pp.name.startswith("test_"):
            tests.append(p)
        else:
            src.append(p)
    return tests, src


def is_candidate(test_files: list[str], gold_files: list[str]) -> bool:
    """同时动了测试与源码才可能是「红转绿」。

    只动测试 → 没有 gold；只动源码 → 没有判定用的 oracle。
    """
    return bool(test_files) and bool(gold_files)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_mine.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval/mine.py tests/test_eval_mine.py
git commit -m "feat(mine): commit 候选筛选

同时动了测试与源码才可能是红转绿：只动测试没有 gold，
只动源码没有判定用的 oracle。

测试文件按目录 + 文件名双判 —— 有的项目把测试和源码放一起，
只按目录判会整片漏掉。"
```

---

### 任务 8：验证并产出任务

**文件：**
- 修改：`src/aifix/eval/mine.py`
- 测试：`tests/test_eval_mine.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_eval_mine.py` 末尾：

```python
import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.eval.mine import mine_tasks, verify_commit


async def test_verify_finds_the_red_to_green_test(history_repo, tmp_path):
    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")
    assert got == [h["target"]]


async def test_verify_rejects_when_nothing_turns_red(history_repo, tmp_path):
    """拿 C 自己当 base：源码已经是好的，测试不会红 —— 不是任务。"""
    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["commit"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")
    assert got == []


async def test_mine_produces_one_task_per_target(history_repo, tmp_path):
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")
    assert len(tasks) == 1
    t = tasks[0]
    assert t.target_test == history_repo["target"]
    assert t.gold_files == ["calc.py"]
    assert t.base_commit == history_repo["base"]
    assert t.commit == history_repo["commit"]
    assert t.test_files == ["tests/test_calc.py"]
    assert history_repo["commit"][:8] in t.task_id


async def test_mine_respects_max_tasks(history_repo, tmp_path):
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, max_tasks=0, workdir=tmp_path / "m")
    assert tasks == []


async def test_mine_skips_root_commit(history_repo, tmp_path):
    """根提交没有父提交，不能构成任务 —— 且不该抛异常。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=99, workdir=tmp_path / "m")
    assert all(t.base_commit for t in tasks)
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_mine.py -q -k "verify or mine"
```

预期：FAIL，`ImportError: cannot import name 'verify_commit'`。

- [ ] **步骤 3：编写实现**

在 `src/aifix/eval/mine.py` 顶部补导入：

```python
import shutil
import subprocess

from ..adapters.pytest_adapter import PytestAdapter
from ..nodes.baseline import run_full_suite
from .task import Task
from .workspace import materialize
```

并在文件末尾追加：

```python
def _git(repo: str | Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()}")
    return res.stdout


def _changed_paths(repo: str, commit: str) -> list[str]:
    out = _git(repo, "show", "--name-only", "--pretty=format:", commit)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _parent(repo: str, commit: str) -> str | None:
    """根提交没有父提交 —— 返回 None 而不是抛。"""
    try:
        return _git(repo, "rev-parse", f"{commit}^").strip()
    except RuntimeError:
        return None


async def verify_commit(repo: str, commit: str, base_commit: str,
                        test_files: list[str], adapter: PytestAdapter,
                        workdir: Path) -> list[str]:
    """返回「在 C^ 处红、在 C 处绿」的用例；不成立返回 []。

    两次全量测试，很贵 —— 但这是 ground truth 的来源，省不得。
    筛选（split_paths / is_candidate）已经把绝大多数 commit 挡在了外面。
    """
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    materialize(repo, base_commit, commit, test_files, workdir)
    red = await run_full_suite(workdir, adapter)
    if not red.ids:
        return []
    _git(workdir, "checkout", "--force", "--quiet", commit)
    green = await run_full_suite(workdir, adapter)
    return sorted(red.ids - green.ids)


async def mine_tasks(repo: str, adapter: PytestAdapter, limit: int = 50,
                     max_tasks: int = 10, workdir: Path | None = None,
                     on_progress=None) -> list[Task]:
    """扫最近 limit 个提交，产出至多 max_tasks 个任务。"""
    workdir = Path(workdir or Path(repo) / ".aifix" / "mine")
    workdir.mkdir(parents=True, exist_ok=True)
    name = Path(repo).name
    tasks: list[Task] = []

    shas = _git(repo, "log", "--no-merges", "--format=%H",
                f"-n{limit}").split()
    for sha in shas:
        if len(tasks) >= max_tasks:
            break
        base = _parent(repo, sha)
        if base is None:
            continue
        test_files, gold_files = split_paths(
            _changed_paths(repo, sha), adapter.test_dirs())
        if not is_candidate(test_files, gold_files):
            continue
        targets = await verify_commit(repo, sha, base, test_files,
                                      adapter, workdir / sha[:8])
        if on_progress:
            on_progress(sha, len(targets))
        for t in targets:
            if len(tasks) >= max_tasks:
                break
            tasks.append(Task(
                task_id=f"{name}@{sha[:8]}::{t}",
                repo=str(Path(repo).resolve()), commit=sha, base_commit=base,
                test_files=test_files, target_test=t, gold_files=gold_files,
                adapter=adapter.name))
    shutil.rmtree(workdir, ignore_errors=True)
    return tasks
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_mine.py -q
```

预期：10 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval/mine.py tests/test_eval_mine.py
git commit -m "feat(mine): 验证并产出任务

两次全量测试确认「C^ 处红、C 处绿」—— 很贵，但这是 ground truth
的来源，省不得。纯函数筛选已经把绝大多数 commit 挡在外面。

根提交没有父提交，跳过而不是抛。"
```

---

# 阶段 4：评测

### 任务 9：单任务执行与打分

**文件：**
- 创建：`src/aifix/eval/runner.py`
- 测试：`tests/test_eval_runner.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_runner.py`：

```python
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.config import AifixConfig
from aifix.eval.runner import run_suite, run_task
from aifix.eval.task import Task

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "high"})
_WRONG_DIAG = json.dumps({
    "suspect_file": "别的文件.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "low"})


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


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _fixer():
    return _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                      _text("已修复")])


def _task(h) -> Task:
    return Task(task_id="hist@t1::test_add", repo=str(h["path"]),
                commit=h["commit"], base_commit=h["base"],
                test_files=h["test_files"], target_test=h["target"],
                gold_files=h["gold_files"])


async def test_successful_task_scores_better_and_locate_hit(
        history_repo, tmp_path):
    r = await run_task(_task(history_repo), AifixConfig(), "假模型",
                       tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.verdict == "better"
    assert r.locate_hit is True           # calc.py ∈ gold_files
    assert r.suspect_file == "calc.py"
    assert r.model == "假模型"
    assert r.error is None
    assert r.tokens > 0


async def test_locate_miss_when_suspect_not_in_gold(history_repo, tmp_path):
    """定位准确率必须对 ground truth 判，不能对 traceback 判。"""
    r = await run_task(_task(history_repo), AifixConfig(), "假模型",
                       tmp_path / "w",
                       detector_client=_Scripted([_text(_WRONG_DIAG)]),
                       fixer_client=_fixer())
    assert r.locate_hit is False
    assert r.suspect_file == "别的文件.py"
    assert r.verdict == "better", "定位错了但改对了 —— 两档分开算"


async def test_baseline_not_reproduced_is_an_error_not_a_failure(
        history_repo, tmp_path):
    """任务本身失效要与「没修好」分开，否则会污染成功率。"""
    t = _task(history_repo).model_copy(
        update={"target_test": "tests/test_calc.py::根本不存在"})
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.error is not None
    assert "复现" in r.error


async def test_suite_isolates_failures(history_repo, tmp_path):
    """一个任务炸掉不能带走整个 suite。"""
    ok = _task(history_repo)
    bad = ok.model_copy(update={"task_id": "坏的", "repo": "/不存在的路径"})
    rs = await run_suite([ok, bad], AifixConfig(), "假模型", tmp_path / "w",
                         parallel=2,
                         detector_client=_Scripted([_text(_DIAG)]),
                         fixer_client=_fixer())
    by_id = {r.task_id: r for r in rs}
    assert by_id["坏的"].error is not None
    assert by_id[ok.task_id].verdict == "better"


async def test_suite_preserves_order(history_repo, tmp_path):
    ts = [_task(history_repo).model_copy(update={"task_id": f"t{i}"})
          for i in range(3)]
    rs = await run_suite(ts, AifixConfig(), "假模型", tmp_path / "w",
                         parallel=2,
                         detector_client=_Scripted([_text(_DIAG)]),
                         fixer_client=_fixer())
    assert [r.task_id for r in rs] == ["t0", "t1", "t2"]
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_runner.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.eval.runner'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/eval/runner.py`：

```python
"""跑任务集：单任务执行与并行调度。

同进程 await run_once，不起子进程 —— 评测跑的必须是产品代码本身，
配置、trace、判定全都是同一套。崩溃隔离由「每个任务包一层 try」解决。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..cli import run_once
from ..config import AifixConfig
from .task import Task, TaskResult
from .workspace import prepare_task_repo

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_id(task_id: str) -> str:
    """run_id 会变成分支名与目录名，必须先洗干净。

    截断后必须补一段哈希：mine 产出的 id 形如
    `proj@abc1234::很长的/路径/test_x.py::test_y`，同一文件里的两个用例
    只在尾部不同，光截断会撞成同一个 id —— 两个任务克隆到同一个目录，
    第二个 git clone 直接失败。
    """
    cleaned = _UNSAFE.sub("_", task_id).strip("_") or "task"
    digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:48]}_{digest}"


def _read_facts(repo: Path, run_id: str) -> list[dict[str, Any]]:
    p = Path(repo) / ".aifix" / "runs" / run_id / "facts.jsonl"
    if not p.is_file():
        return []
    return [json.loads(x) for x in
            p.read_text(encoding="utf-8").splitlines() if x.strip()]


async def run_task(task: Task, config: AifixConfig, model: str, workdir: Path,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> TaskResult:
    run_id = _safe_id(task.task_id)
    dest = Path(workdir) / run_id
    blank = TaskResult(task_id=task.task_id, model=model, locate_hit=False,
                       suspect_file=None, verdict="same", attempts=0,
                       tokens=0, cost_usd=0.0, violations=0)

    prepare_task_repo(task, dest)
    state = await run_once(dest, config, run_id=run_id,
                           only_test=task.target_test,
                           detector_client=detector_client,
                           fixer_client=fixer_client)

    if task.target_test not in state["baseline_ids"]:
        # 任务失效（源仓库变了、环境不同、测试本身不稳定）。这与「没修好」
        # 是两回事 —— 混进成功率会让被测系统替评测的问题背锅。
        return blank.model_copy(update={
            "error": f"baseline 未复现目标用例：{task.target_test}"})

    facts = _read_facts(dest, run_id)
    suspect = next((f["value"] for f in facts if f["key"] == "suspect_file"),
                   None)
    violations = sum(1 for f in facts if f["key"] == "violation")
    row = next((r for r in state["results"]
                if r["test_id"] == task.target_test), None)

    return TaskResult(
        task_id=task.task_id, model=model,
        # 规格 §9 的定义：对 ground truth 判，不是对 traceback 判
        locate_hit=suspect in task.gold_files if suspect else False,
        suspect_file=suspect,
        verdict=row["verdict"] if row else "same",
        attempts=row["attempts"] if row else 0,
        tokens=state["spent_tokens"], cost_usd=state["spent_usd"],
        violations=violations,
        abort_reason=(row or {}).get("abort_reason") or state.get("abort"),
    )


async def run_suite(tasks: list[Task], config: AifixConfig, model: str,
                    workdir: Path, parallel: int = 4,
                    detector_client: Any = None,
                    fixer_client: Any = None,
                    on_done=None) -> list[TaskResult]:
    """并行跑整个任务集。返回顺序与传入顺序一致。"""
    sem = asyncio.Semaphore(parallel)

    async def one(t: Task) -> TaskResult:
        async with sem:
            try:
                r = await run_task(t, config, model, workdir,
                                   detector_client=detector_client,
                                   fixer_client=fixer_client)
            except Exception as e:      # 一个任务炸掉不能带走整个 suite
                r = TaskResult(task_id=t.task_id, model=model,
                               locate_hit=False, suspect_file=None,
                               verdict="same", attempts=0, tokens=0,
                               cost_usd=0.0, violations=0, error=repr(e))
            if on_done:
                on_done(r)
            return r

    return list(await asyncio.gather(*(one(t) for t in tasks)))
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_runner.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval/runner.py tests/test_eval_runner.py
git commit -m "feat(eval): 单任务执行与并行调度

locate_hit 对 gold_files 判 —— 规格 §9 的定义。定位与修复两档
分开算：定位错了但改对了是常见情形，合成一个数字就看不见了。

baseline 未复现记成 error 而非失败：任务失效是评测自己的问题，
混进成功率会让被测系统替评测背锅。

一个任务炸掉不能带走整个 suite。"
```

---

### 任务 10：双档打分与跨模型对比表

**文件：**
- 创建：`src/aifix/eval/score.py`
- 测试：`tests/test_eval_score.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_score.py`：

```python
from aifix.eval.score import render_table, summarize
from aifix.eval.task import TaskResult


def _r(**over):
    base = dict(task_id="t", model="M", locate_hit=True, suspect_file="a.py",
                verdict="better", attempts=1, tokens=1000, cost_usd=0.10,
                violations=0)
    base.update(over)
    return TaskResult(**base)


def test_rates_are_over_valid_tasks_only():
    """出错的任务不计入分母 —— 否则评测自己的故障会拉低被测系统的成绩。"""
    s = summarize([_r(), _r(verdict="same", locate_hit=False),
                   _r(error="克隆失败")])
    assert s.tasks == 2
    assert s.errors == 1
    assert s.fix_rate == 0.5
    assert s.locate_rate == 0.5


def test_all_errors_gives_zero_rates_not_crash():
    s = summarize([_r(error="炸了")])
    assert s.tasks == 0
    assert s.fix_rate == 0.0
    assert s.locate_rate == 0.0


def test_empty_input():
    s = summarize([])
    assert s.tasks == 0
    assert s.model == ""


def test_averages_and_totals():
    s = summarize([_r(cost_usd=0.10, attempts=1, violations=2),
                   _r(cost_usd=0.30, attempts=3, violations=1)])
    assert abs(s.avg_cost_usd - 0.20) < 1e-9
    assert abs(s.avg_attempts - 2.0) < 1e-9
    assert s.violations == 3, "越界是总数不是均值 —— 关心的是有没有、有几次"


def test_model_taken_from_results():
    assert summarize([_r(model="deepseek-v4-pro")]).model == "deepseek-v4-pro"


def test_table_has_one_row_per_model():
    a = summarize([_r(model="A")])
    b = summarize([_r(model="B", verdict="same")])
    md = render_table([a, b])
    assert "| A |" in md
    assert "| B |" in md
    assert "定位准确率" in md
    assert "越界尝试" in md


def test_table_marks_error_count():
    """评测故障要单列出来 —— 不进分母，但也不能藏起来。"""
    s = summarize([_r(), _r(error="炸了")])
    row = render_table([s]).strip().splitlines()[-1]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "1", f"有效任务数应为 1：{row}"
    assert cells[-1] == "1", f"评测故障数应为 1：{row}"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_score.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.eval.score'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/eval/score.py`：

```python
"""双档打分与跨模型对比表（规格 §9）。

- 定位准确率 = locate_hit 占比 → Detector 的能力
- 修复成功率 = verdict == better 占比 → 整体能力

两档分开：定位错了但改对了是常见情形（模型自己读代码纠正了诊断），
合成一个数字就看不见 Detector 到底有没有用。
"""
from __future__ import annotations

from pydantic import BaseModel

from .task import TaskResult


class Summary(BaseModel):
    model: str
    tasks: int                  # 有效任务数（不含出错的）
    locate_rate: float
    fix_rate: float
    avg_cost_usd: float
    avg_attempts: float
    violations: int             # 总次数，不是均值
    errors: int


def summarize(results: list[TaskResult]) -> Summary:
    model = results[0].model if results else ""
    # 出错的任务不进分母：那是评测自己的故障，不该拉低被测系统的成绩
    valid = [r for r in results if r.error is None]
    n = len(valid)
    if n == 0:
        return Summary(model=model, tasks=0, locate_rate=0.0, fix_rate=0.0,
                       avg_cost_usd=0.0, avg_attempts=0.0,
                       violations=sum(r.violations for r in results),
                       errors=len(results) - n)
    return Summary(
        model=model, tasks=n,
        locate_rate=sum(r.locate_hit for r in valid) / n,
        fix_rate=sum(r.verdict == "better" for r in valid) / n,
        avg_cost_usd=sum(r.cost_usd for r in valid) / n,
        avg_attempts=sum(r.attempts for r in valid) / n,
        violations=sum(r.violations for r in valid),
        errors=len(results) - n,
    )


def render_table(summaries: list[Summary]) -> str:
    lines = [
        "| 模型 | 任务数 | 定位准确率 | 修复成功率 | 平均成本 | 平均尝试 |"
        " 越界尝试 | 评测故障 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.model} | {s.tasks} | {s.locate_rate:.0%} | {s.fix_rate:.0%}"
            f" | ${s.avg_cost_usd:.3f} | {s.avg_attempts:.1f}"
            f" | {s.violations} | {s.errors} |")
    return "\n".join(lines) + "\n"
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_score.py -q
```

预期：7 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/eval/score.py tests/test_eval_score.py
git commit -m "feat(eval): 双档打分与跨模型对比表

出错的任务不进分母 —— 评测自己的故障不该拉低被测系统的成绩，
但也不能藏起来，所以单列一栏。

越界记总数不是均值：关心的是有没有、有几次。"
```

---

### 任务 11：CLI 三个子命令

**文件：**
- 修改：`src/aifix/cli.py`
- 测试：`tests/test_cli_args.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_cli_args.py` 末尾：

```python
def test_mine_subcommand():
    a = build_parser().parse_args(
        ["mine", "/tmp/proj", "--limit", "80", "--max-tasks", "5",
         "--out", "e/t.jsonl"])
    assert a.cmd == "mine"
    assert a.repo == "/tmp/proj"
    assert a.limit == 80
    assert a.max_tasks == 5
    assert a.out == "e/t.jsonl"


def test_mine_defaults():
    a = build_parser().parse_args(["mine"])
    assert a.repo == "."
    assert a.limit == 50
    assert a.max_tasks == 10


def test_eval_subcommand():
    a = build_parser().parse_args(
        ["eval", "e/t.jsonl", "--parallel", "8", "--label", "pro",
         "--out", "e/r.jsonl"])
    assert a.cmd == "eval"
    assert a.tasks == "e/t.jsonl"
    assert a.parallel == 8
    assert a.label == "pro"


def test_eval_requires_task_file():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval"])


def test_eval_report_takes_many_result_files():
    a = build_parser().parse_args(["eval-report", "a.jsonl", "b.jsonl"])
    assert a.cmd == "eval-report"
    assert a.results == ["a.jsonl", "b.jsonl"]


def test_eval_report_requires_at_least_one():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval-report"])
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_cli_args.py -q -k "mine or eval"
```

预期：FAIL，`argparse` 拒绝未知子命令 `mine`（`SystemExit: 2`）。

- [ ] **步骤 3：编写实现**

把 `src/aifix/cli.py` 的 `build_parser` 中 `return parser` 之前插入：

```python
    mine = sub.add_parser("mine", help="从 git history 挖任务集")
    mine.add_argument("repo", nargs="?", default=".")
    mine.add_argument("--limit", type=int, default=50,
                      help="回溯多少个提交")
    mine.add_argument("--max-tasks", type=int, default=10,
                      help="最多产出多少个任务")
    mine.add_argument("--out", default="evals/tasks.jsonl")

    ev = sub.add_parser("eval", help="在任务集上跑评测")
    ev.add_argument("tasks")
    ev.add_argument("--parallel", type=int, default=4)
    ev.add_argument("--label", default=None,
                    help="这一轮的模型标签，默认取 fixer 的 model")
    ev.add_argument("--out", default=None)

    rep = sub.add_parser("eval-report", help="把若干轮结果渲染成对比表")
    rep.add_argument("results", nargs="+")
```

并把 `main` 替换为：

```python
def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "run":
        _cmd_run(args)
    elif args.cmd == "mine":
        _cmd_mine(args)
    elif args.cmd == "eval":
        _cmd_eval(args)
    elif args.cmd == "eval-report":
        _cmd_eval_report(args)


def _cmd_run(args) -> None:
    config = AifixConfig()
    if args.budget is not None:
        config = config.model_copy(update={"budget_usd": args.budget})
    state = asyncio.run(run_once(
        Path(args.repo).resolve(), config, run_id=uuid.uuid4().hex[:8],
        only_test=args.test, dry_run=args.dry_run))
    print(state["report_md"])


def _cmd_mine(args) -> None:
    from .adapters.pytest_adapter import PytestAdapter
    from .eval.mine import mine_tasks
    from .eval.task import write_jsonl

    def progress(sha: str, n: int) -> None:
        print(f"  {sha[:8]}：{n} 个可用用例", flush=True)

    tasks = asyncio.run(mine_tasks(
        str(Path(args.repo).resolve()), PytestAdapter(),
        limit=args.limit, max_tasks=args.max_tasks, on_progress=progress))
    write_jsonl(Path(args.out), tasks)
    print(f"产出 {len(tasks)} 个任务 → {args.out}")


def _cmd_eval(args) -> None:
    from .eval.runner import run_suite
    from .eval.score import render_table, summarize
    from .eval.task import Task, TaskResult, read_jsonl, write_jsonl

    config = AifixConfig()
    label = args.label or config.fixer.model or "未命名"
    tasks = read_jsonl(Path(args.tasks), Task)
    workdir = Path(tempfile.mkdtemp(prefix="aifix-eval-"))

    def done(r: TaskResult) -> None:
        mark = "✅" if r.verdict == "better" else ("⚠️" if r.error else "❌")
        print(f"  {mark} {r.task_id}", flush=True)

    print(f"{len(tasks)} 个任务 · {label} · 并行 {args.parallel}")
    results = asyncio.run(run_suite(tasks, config, label, workdir,
                                    parallel=args.parallel, on_done=done))
    out = Path(args.out or f"evals/results-{label}.jsonl")
    write_jsonl(out, results)
    print()
    print(render_table([summarize(results)]))
    print(f"明细 → {out}")
    shutil.rmtree(workdir, ignore_errors=True)


def _cmd_eval_report(args) -> None:
    from .eval.score import render_table, summarize
    from .eval.task import TaskResult, read_jsonl

    summaries = [summarize(read_jsonl(Path(p), TaskResult))
                 for p in args.results]
    print(render_table(summaries))
```

并在 `cli.py` 顶部导入中补上：

```python
import shutil
import tempfile
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_cli_args.py -q
```

预期：13 passed。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/cli.py tests/test_cli_args.py
git commit -m "feat(cli): mine / eval / eval-report

eval-report 单独成命令：跨模型对比要的是把几轮结果并排看，
而每一轮是在不同时间、不同配置下跑的。"
```

---

# 阶段 5：端到端

### 任务 12：挖掘到对比表的完整链路

**文件：**
- 测试：`tests/test_eval_e2e.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_eval_e2e.py`：

```python
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.eval.mine import mine_tasks
from aifix.eval.runner import run_suite
from aifix.eval.score import render_table, summarize
from aifix.eval.task import Task, TaskResult, read_jsonl, write_jsonl

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "high"})


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


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


async def test_mine_then_eval_then_table(history_repo, tmp_path):
    """挖 → 跑 → 打分 → 出表，全链路一次跑通。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")
    assert len(tasks) == 1

    tasks_file = tmp_path / "evals" / "tasks.jsonl"
    write_jsonl(tasks_file, tasks)
    assert read_jsonl(tasks_file, Task) == tasks

    results = await run_suite(
        read_jsonl(tasks_file, Task), AifixConfig(), "假模型",
        tmp_path / "w", parallel=2,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))

    results_file = tmp_path / "evals" / "results.jsonl"
    write_jsonl(results_file, results)
    back = read_jsonl(results_file, TaskResult)

    s = summarize(back)
    assert s.tasks == 1
    assert s.fix_rate == 1.0
    assert s.locate_rate == 1.0
    assert s.errors == 0

    md = render_table([s])
    assert "假模型" in md
    assert "100%" in md


async def test_two_labels_render_side_by_side(history_repo, tmp_path):
    """跨模型对比的形状：同一批任务，两轮结果并排。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")

    good = await run_suite(
        tasks, AifixConfig(), "会修的", tmp_path / "a", parallel=1,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    lazy = await run_suite(
        tasks, AifixConfig(), "不修的", tmp_path / "b", parallel=1,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_text("我觉得没问题")]))

    sg, sl = summarize(good), summarize(lazy)
    assert sg.fix_rate == 1.0
    assert sl.fix_rate == 0.0
    # 两边定位都对：差别只在改没改。这正是双档分开的意义 ——
    # 合成一个数字就看不出「Detector 没问题，是 Fixer 不干活」。
    assert sg.locate_rate == sl.locate_rate == 1.0

    md = render_table([sg, sl])
    rows = [ln for ln in md.splitlines() if ln.startswith("| 会修的")
            or ln.startswith("| 不修的")]
    assert len(rows) == 2
    # 注意：不能写 `"0%" in row` —— 它是 "100%" 的子串，那样的断言恒为真。
    def _cells(r):
        return [c.strip() for c in r.strip("|").split("|")]
    assert _cells(rows[0])[3] == "100%", rows[0]
    assert _cells(rows[1])[3] == "0%", rows[1]
```

- [ ] **步骤 2：运行测试**

```bash
uv run pytest tests/test_eval_e2e.py -q
```

预期：2 passed（前面 11 个任务都做对的话，这里不需要新代码）。若失败，说明某个环节的接口对不上——按报错定位，不要在这里加特例。

- [ ] **步骤 3：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 4：Commit**

```bash
git add tests/test_eval_e2e.py
git commit -m "test(eval): 挖 → 跑 → 打分 → 出表全链路

第二个用例是跨模型对比的形状：同一批任务，一个会修一个不修，
两行并排 —— 表能把差别显示出来，这张表才有存在的意义。"
```

---

## M3 完成标志

- [ ] `uv run pytest -q` 全绿
- [ ] `aifix mine <真实 pytest 项目> --limit 200 --max-tasks 5` 产出非空任务集，且人工抽查一个任务：`base_commit` 处该用例确实红、`commit` 处确实绿
- [ ] `aifix eval evals/tasks.jsonl --label deepseek-v4-pro` 跑通，输出对比表与明细 jsonl
- [ ] 换 `AIFIX_FIXER__MODEL` 再跑一轮，`aifix eval-report` 把两轮并排渲染出**第一张跨模型对比表**（规格 §13 的 M3 验收）
- [ ] 表里「越界尝试」一列在至少一个模型上非零——否则这一列没有被真正验证过

最后三条需手动执行：

```bash
export AIFIX_FIXER__API_KEY=... AIFIX_FIXER__BASE_URL=https://api.deepseek.com
export AIFIX_DETECTOR__API_KEY=$AIFIX_FIXER__API_KEY \
       AIFIX_DETECTOR__BASE_URL=$AIFIX_FIXER__BASE_URL \
       AIFIX_DETECTOR__MODEL=deepseek-v4-flash

aifix mine ~/some/pytest-project --limit 200 --max-tasks 5 --out evals/tasks.jsonl

AIFIX_FIXER__MODEL=deepseek-v4-pro \
  aifix eval evals/tasks.jsonl --label pro   --out evals/pro.jsonl
AIFIX_FIXER__MODEL=deepseek-v4-flash \
  aifix eval evals/tasks.jsonl --label flash --out evals/flash.jsonl

aifix eval-report evals/pro.jsonl evals/flash.jsonl
```

## 交给 M3b 的缺口

| 缺口 | 说明 |
|---|---|
| `MavenAdapter` | 验证适配层抽象是否真的成立。放在评测之后：有了任务集才能证明它确实能跑通，而不只是「编译过了」 |
| `aifix replay` | 消费 `events.jsonl` 做逐步重演。诊断工具，不影响任何指标 |
| `abort_reason` 进报告 | M2 遗留：未到 `max_attempts` 时守卫的 `abort_reason` 只在 facts 里。本计划的 `TaskResult.abort_reason` 已从 facts + state 取到，报告侧仍未补 |
| SQLite 轨迹 | M2 推迟至今。本计划用 jsonl 撑住了单 suite 的分析；跨 suite、跨时间的聚合仍缺一张表 |
| C 类冒烟集 | 人造变异生成器（规格 §9）。先用 `mine --max-tasks 5` 顶替；等真跑过一轮、确认 A 类耗时确实是瓶颈了再补 |
| 公开数据集 | SWE-bench Lite / Defects4J，规格 §9 列为第二阶段 |
| `make_test_id` 对类内测试产出无效 id | pytest 默认 `junit_family=xunit2` 不写 `<testcase file="...">` 属性，`make_test_id` 缺失 file 时走 `classname.replace(".", "/") + ".py"` 回退路径，对 `test_foo.TestBar::test_baz` 这类类内测试会拼出不存在的路径。M1 遗留，影响面不止挖掘：M2 的 flaky 过滤（`run_scoped` 复跑）和 `run_tests` 工具同样依赖这个 id 能对上真实路径。修法方向是让适配器优先用 junit 的 `file` 属性，缺失时再从 classname 推断类名段，而不是整段替换成路径 |
| `split_paths` 只处理 `.py` | 若某个 commit 同时新增了测试所需的非 `.py` 夹具文件（如数据文件、配置片段），该文件不会被 `materialize` 嫁接到任务工作区，任务会在 base 侧因缺文件而红、在 C 侧绿，通过全部现有校验进入任务集，但 ground truth 实际不可达。这不是「捏造任务」——确实是红转绿——而是任务质量问题：修复模型即便诊断和补丁都对，也可能因为缺夹具文件而通不过 |
