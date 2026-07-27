# M2 靠谱 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 M1 的闭环在异常输入下不崩、不越界、不烧穿预算，且每一步可从 trace 复盘。

**架构：** 在既有六节点之上补齐六条属于 app 的安全闸；新增 `trace.py`（三层嵌套 span + 事件落盘）与 `budget.py`（三层动态分配）两个模块；`cli.run_once` 从手工驱动改为由 LangGraph 图驱动，接上 `SqliteSaver` 实现断点续跑。

**技术栈：** Python ≥3.11 · `ai-harness-framework` 0.0.2（PyPI）· `langgraph` 1.2 · `langgraph-checkpoint-sqlite` 3.1 · OpenTelemetry · pytest

**规格：** `docs/superpowers/specs/2026-07-27-ai-fix-code-loop-design.md`
**前置：** M1 已完成并合并（`main` @ 94 passed）

## 起点：M1 遗留的已知隐患

| # | 隐患 | 来源 |
|---|---|---|
| 1 | `Worktree.commit` 用 `git add -A`，构建产物会进交付分支。M1 加 `python -B` 只挡住 `.pyc` | 真实运行发现 |
| 2 | 空 diff / 巨型 diff 无守卫 | 规格 §5，M1 明确延后 |
| 3 | flaky 无过滤，抖动会误回滚正确补丁 | 规格 §7 |
| 4 | 无熔断，系统性失败会匀速烧钱 | 规格 §10 |
| 5 | 预算只做粗略扣减，未按剩余 failure 动态分配 | 规格 §10 |
| 6 | 无 trace、无 events.jsonl、无轨迹落库 | 规格 §8 |
| 7 | `build_graph()` 已就位但 `run_once` 未使用，无断点续跑 | M1 遗留 |

## 与规格的偏差（自检发现，已确认）

规格 §8 列了三份产物：`events.jsonl`、**SQLite 轨迹**、`report.md`。本计划做前后两份，外加一份 `facts.jsonl`，**SQLite 轨迹推迟到 M3**。

理由：SQLite 的价值在**跨 run 聚合**（"这个月修了多少、花了多少钱、哪类失败最修不好"）。M2 的目标是"单次 run 出问题能复盘"，per-run 的 jsonl 已经够；跨 run 聚合的第一个真实消费者是 M3 的评测，届时连同任务集格式一起设计更合理，现在建表等于凭空猜 schema。

`facts.jsonl` 不在规格里，是本计划新增的：把**领域判断结论**（verdict / rollback / flaky / locate_hit）与**模型原始事件**分开两个文件。混在一起的话，评测要从几万行事件里筛出那几条结论，而这两类数据的消费者和生命周期完全不同。

## 文件结构

**新建**

| 文件 | 职责 |
|---|---|
| `src/aifix/trace.py` | 三层嵌套 span、领域属性、`events.jsonl` 落盘、SQLite 轨迹 |
| `src/aifix/budget.py` | 三层预算分配与记账 |

**修改**

| 文件 | 变更 |
|---|---|
| `src/aifix/tools/patch.py` | 记录本次 run 中被 patch 触及的路径 |
| `src/aifix/agents/fixer.py` | `build_registry` 返回路径收集器 |
| `src/aifix/delivery.py` | `commit` 只提交明确改过的文件 |
| `src/aifix/nodes/fix.py` | 空 diff / 巨型 diff 守卫（含带反馈重试） |
| `src/aifix/nodes/verify.py` | flaky 过滤 + 领域属性 |
| `src/aifix/nodes/detect.py` | `locate_hit` 属性 |
| `src/aifix/nodes/report.py` | 报告与产物写入 `.aifix/runs/<run_id>/` |
| `src/aifix/graph.py` | 熔断路由 |
| `src/aifix/config.py` | trace / 守卫 / 熔断相关字段 |
| `src/aifix/cli.py` | 预算分配、trace 接线、改由图驱动 + checkpointer |

**框架侧：无改动。** M2 全部在 app 层完成，`ai-harness-framework` 0.0.2 不需要任何变更。

---

# 阶段 1：交付纯净性

### 任务 1：`ApplyPatchTool` 记录触及的路径

`git add -A` 的根治办法是让 commit 只提交 agent **明确改过**的文件。`apply_patch` 是唯一的修改手段，它知道每个 diff 的目标路径。

**文件：**
- 修改：`src/aifix/tools/patch.py`
- 测试：`tests/test_tool_patch.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_tool_patch.py` 末尾：

```python
async def test_records_touched_paths(buggy_repo):
    """apply_patch 是唯一的修改手段，它必须记下自己动过哪些文件。"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        touched: set[str] = set()
        reg = ToolRegistry()
        reg.register(ApplyPatchTool(sb, test_dirs=["tests"], touched=touched))
        ex = ToolExecutor(reg, max_chars=8000)
        r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                      arguments={"diff": _GOOD}))
        assert not r.is_error, r.content
        assert touched == {"calc.py"}
    finally:
        await sb.close()


async def test_rejected_patch_records_nothing(buggy_repo):
    """被拒绝的补丁不该留下痕迹 —— 它一个字节都没写进去。"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        touched: set[str] = set()
        reg = ToolRegistry()
        reg.register(ApplyPatchTool(sb, test_dirs=["tests"], touched=touched))
        ex = ToolExecutor(reg, max_chars=8000)
        await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _TOUCHES_TEST}))
        await ex.execute(ToolCall(id="2", name="apply_patch",
                                  arguments={"diff": _BAD_CONTEXT}))
        assert touched == set()
    finally:
        await sb.close()


async def test_touched_is_optional(buggy_repo):
    """不传 touched 时行为不变（现有调用点不受影响）。"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = ToolRegistry()
        reg.register(ApplyPatchTool(sb, test_dirs=["tests"]))
        ex = ToolExecutor(reg, max_chars=8000)
        r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                      arguments={"diff": _GOOD}))
        assert not r.is_error
    finally:
        await sb.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_tool_patch.py -q -k "touched"
```

预期：FAIL，`TypeError: ApplyPatchTool.__init__() got an unexpected keyword argument 'touched'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/tools/patch.py` 的 `__init__` 与 `run` 改为：

```python
    def __init__(self, sandbox: Sandbox, test_dirs: list[str],
                 timeout: float = 60.0, touched: set[str] | None = None) -> None:
        self._sandbox = sandbox
        self._test_dirs = [d.strip("/") for d in test_dirs]
        self._timeout = timeout
        # 本次 run 中被成功应用的补丁触及的路径。交付时只提交这些文件，
        # 避免 git add -A 把测试产物、缓存等未跟踪垃圾扫进分支。
        self._touched = touched

    async def run(self, params: "ApplyPatchTool.Params") -> str:
        targets = self._targets(params.diff)
        try:
            self._guard(targets)
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
            # 只有真正写进去了才记账
            if self._touched is not None:
                self._touched.update(targets)
            stat = await self._sandbox.exec(
                ["git", "diff", "--stat"], self._timeout)
            return "补丁已应用。当前改动：\n" + (stat.stdout.strip() or "（无）")
        finally:
            await self._sandbox.exec(["rm", "-f", _PATCH_FILE], 10.0)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_tool_patch.py -q
```

预期：8 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/tools/patch.py tests/test_tool_patch.py
git commit -m "feat(tools): ApplyPatchTool 记录成功应用的补丁触及的路径

只在补丁真正写进去后记账，被拒绝或打不上的不留痕迹。
供交付阶段精确提交，替代 git add -A。"
```

---

### 任务 2：`Worktree.commit` 只提交指定路径

**文件：**
- 修改：`src/aifix/delivery.py`
- 测试：`tests/test_delivery.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_delivery.py` 末尾：

```python
def test_commit_only_stages_given_paths(buggy_repo, fixed_source):
    """构建产物不该进交付分支 —— 真实运行中 .pyc 被 git add -A 扫了进去。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        (wt.path / "junk.log").write_text("构建产物", encoding="utf-8")
        (wt.path / "__pycache__").mkdir()
        (wt.path / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        wt.commit("fix: x", paths=["calc.py"])
        tracked = _git(wt.path, "ls-tree", "-r", "--name-only", "HEAD")
        assert "calc.py" in tracked
        assert "junk.log" not in tracked
        assert "__pycache__/x.pyc" not in tracked


def test_commit_stages_new_file_when_listed(buggy_repo):
    """agent 新建的源文件必须能提交（它不在已跟踪集合里）。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
        wt.commit("feat: helper", paths=["helper.py"])
        tracked = _git(wt.path, "ls-tree", "-r", "--name-only", "HEAD")
        assert "helper.py" in tracked


def test_commit_with_empty_paths_is_noop(buggy_repo, fixed_source):
    """没有明确改过的文件就不该产生提交。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        before = _git(wt.path, "rev-parse", "HEAD").strip()
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.commit("fix: x", paths=[])
        assert _git(wt.path, "rev-parse", "HEAD").strip() == before
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_delivery.py -q -k "only_stages or new_file_when_listed or empty_paths"
```

预期：FAIL，`TypeError: Worktree.commit() got an unexpected keyword argument 'paths'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/delivery.py` 的 `commit` 方法替换为：

```python
    def commit(self, message: str, paths: list[str]) -> None:
        """只提交 `paths` 里明确列出的文件。

        绝不用 `git add -A`：worktree 里跑测试会产生未跟踪产物
        （__pycache__、覆盖率文件、日志……），全扫进去会污染交付分支。
        paths 来自 ApplyPatchTool 的记账 —— 它是 agent 唯一的修改手段，
        知道自己动过哪些文件。
        """
        if not paths:
            return
        _git(self.path, "add", "--", *paths)
        _git(self.path, "commit", "-q", "-m", message)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_delivery.py -q
```

预期：`test_commit_keeps_changes_and_rollback_after_is_noop` FAIL（旧签名），其余 PASS。

- [ ] **步骤 5：更新旧测试到新签名**

把 `tests/test_delivery.py` 里的

```python
        wt.commit("fix: test_add")
```

改为

```python
        wt.commit("fix: test_add", paths=["calc.py"])
```

- [ ] **步骤 6：运行测试验证通过**

```bash
uv run pytest tests/test_delivery.py -q
```

预期：13 passed。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/delivery.py tests/test_delivery.py
git commit -m "fix(delivery): commit 只提交明确改过的文件

根治 git add -A 的隐患：worktree 里跑测试会产生未跟踪产物，
全扫进去会污染交付分支。M1 加 python -B 只挡住了 .pyc，
覆盖率文件、日志等仍会进去。

paths 来自 ApplyPatchTool 的记账 —— agent 唯一的修改手段
知道自己动过哪些文件。空列表不产生提交。"
```

---

### 任务 3：`build_registry` 贯通路径收集器

**文件：**
- 修改：`src/aifix/agents/fixer.py`
- 测试：`tests/test_fixer.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_fixer.py` 末尾：

```python
async def test_registry_wires_touched_collector(buggy_repo):
    """收集器要真的接到 apply_patch 上，否则交付阶段拿不到路径。"""
    from harness.tools.base import ToolExecutor
    from harness.types import ToolCall

    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        touched: set[str] = set()
        reg = build_registry(sb, PytestAdapter(), known_ids=set(), touched=touched)
        ex = ToolExecutor(reg, max_chars=8000)
        r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                      arguments={"diff": patch}))
        assert not r.is_error, r.content
        assert touched == {"calc.py"}
    finally:
        await sb.close()


async def test_registry_touched_is_optional(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids=set())
        assert reg.get("apply_patch") is not None
    finally:
        await sb.close()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_fixer.py -q -k "touched"
```

预期：FAIL，`TypeError: build_registry() got an unexpected keyword argument 'touched'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/agents/fixer.py` 的 `build_registry` 替换为：

```python
def build_registry(sandbox: Sandbox, adapter: ProjectAdapter,
                   known_ids: set[str],
                   touched: set[str] | None = None) -> ToolRegistry:
    """Fixer 的能力面：白名单，五个工具，没有 shell。

    touched：传入一个集合，apply_patch 会把成功改动的路径记进去，
    供交付阶段精确提交（见 Worktree.commit）。
    """
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    reg.register(ApplyPatchTool(sandbox, test_dirs=adapter.test_dirs(),
                                touched=touched))
    reg.register(RunTestsTool(sandbox, adapter, known_ids=known_ids))
    return reg
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_fixer.py -q
```

预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/agents/fixer.py tests/test_fixer.py
git commit -m "feat(agents): build_registry 贯通路径收集器到 apply_patch"
```

---

# 阶段 2：守卫与判定

### 任务 4：空 diff 与巨型 diff 守卫

模型宣称修好却一字未改，是这个领域最常见也最隐蔽的失败；改动过大则说明它放弃理解、直接重写整个文件——那种补丁即使测试转绿也不该合。

**文件：**
- 修改：`src/aifix/nodes/fix.py`、`src/aifix/config.py`
- 测试：`tests/test_nodes_fix_guards.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_nodes_fix_guards.py`：

```python
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.fix import fix_node
from aifix.nodes.preflight import preflight_node

_TID = "tests/test_calc.py::test_add"

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
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
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


def _state(buggy_repo, wt, **over):
    cfg = AifixConfig(**over)
    st = new_state(buggy_repo, cfg, run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["baseline_ids"] = [_TID]
    st["current"] = _TID
    st["attempt"] = 1
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="c", name="test_add",
        message="assert -1 == 5", trace="E assert -1 == 5")}
    return st


async def test_empty_diff_triggers_retry_with_feedback(buggy_repo):
    """只说「已修复」却没调 apply_patch —— 必须重试并把这件事告诉模型。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        client = _Scripted([_text("已修复"), _tool("apply_patch",
                                                   json.dumps({"diff": _PATCH}))])
        out = await fix_node(st, client=client)
        assert client.calls == 2, "空 diff 后应再给模型一次机会"
        assert out["touched"] == ["calc.py"]
        assert out["guard_hits"] == ["empty_diff"]


async def test_empty_diff_exhausts_retries(buggy_repo):
    """一直不改就放弃，并记下原因。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=1)
        client = _Scripted([_text("已修复")])
        out = await fix_node(st, client=client)
        assert client.calls == 2
        assert out["touched"] == []
        assert out["abort_reason"] == "empty_diff"


async def test_huge_diff_rolls_back_and_retries(buggy_repo):
    """改动过大判为整文件重写：回滚后重来，不能让垃圾留在工作区。"""
    big = "\n".join(f"+line{i}" for i in range(400))
    huge = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,402 @@\n"
            " def add(a, b):\n-    return a - b        # bug: 应为 a + b\n"
            + big + "\n")
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, max_diff_lines=50)
        client = _Scripted([
            _tool("apply_patch", json.dumps({"diff": huge})),
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
        ])
        out = await fix_node(st, client=client)
        assert "huge_diff" in out["guard_hits"]
        content = (wt.path / "calc.py").read_text(encoding="utf-8")
        assert "line399" not in content, "巨型补丁必须被回滚"
        assert "a + b" in content, "回滚后第二次补丁应正常应用"


async def test_good_patch_passes_guards_in_one_shot(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH}))])
        out = await fix_node(st, client=client)
        assert client.calls == 1
        assert out["guard_hits"] == []
        assert out["abort_reason"] is None
        assert out["diff_lines"] > 0
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q
```

预期：FAIL，`TypeError: AifixConfig() got an unexpected keyword argument 'fix_guard_retries'`。

- [ ] **步骤 3：给 `AifixConfig` 加守卫字段**

在 `src/aifix/config.py` 的 `max_diff_lines` 之后插入：

```python
    # 守卫触发后额外给模型的重试次数（不计入 max_attempts）
    fix_guard_retries: int = 2
```

- [ ] **步骤 4：重写 `fix_node`**

把 `src/aifix/nodes/fix.py` 整个替换为：

```python
from __future__ import annotations

from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.sandbox.local import LocalSandbox
from harness.types import Message, Role

from ..agents.detector import Diagnosis
from ..agents.fixer import SYSTEM_PROMPT, build_initial_messages, build_registry
from ..agents.runner import consume
from ..graph import AifixState
from .baseline import adapter_for

_EMPTY_FEEDBACK = (
    "你没有对任何文件做出修改。只说「已修复」是无效的 —— "
    "请先用 read_file 确认文件当前的真实内容，再用 apply_patch 提交具体改动。")

_HUGE_FEEDBACK = (
    "你的改动范围过大（{lines} 行，上限 {limit} 行），疑似整文件重写。"
    "改动已被回滚。请只改必要的那几行，用最小的 diff 修复问题。")


async def _diff_lines(sandbox: LocalSandbox) -> int:
    """工作区当前改动的行数（+/- 行）。"""
    res = await sandbox.exec(["git", "diff", "--numstat"], 30.0)
    total = 0
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    return total


async def _rollback(sandbox: LocalSandbox) -> None:
    await sandbox.exec(["git", "checkout", "--", "."], 60.0)
    await sandbox.exec(["git", "clean", "-fd"], 60.0)


async def fix_node(state: AifixState, client: Any = None) -> dict[str, Any]:
    """跑 Fixer，并在其结束后检查改动是否合理。

    两条守卫都以「带反馈重试」的方式处理，而不是直接失败：模型拿到
    具体的问题描述后，下一次通常就对了。守卫重试不计入 attempt ——
    attempt 衡量的是「修复尝试」，而这里连一次有效尝试都还没产生。
    """
    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    adapter = adapter_for(state["adapter_name"])
    raw = state.get("diagnosis")
    diagnosis = Diagnosis.model_validate(raw) if raw else None

    remaining = max(cfg.budget_tokens - state["spent_tokens"], 10_000)
    touched: set[str] = set()
    guard_hits: list[str] = []
    abort_reason: str | None = None
    tokens = cost = 0.0
    lines = 0

    sandbox = LocalSandbox(workspace=state["worktree_path"])
    await sandbox.start()
    try:
        loop = AgentLoop(
            client=client or OpenAICompatibleClient(cfg.fixer),
            registry=build_registry(sandbox, adapter,
                                    known_ids=set(state["baseline_ids"]),
                                    touched=touched),
            context=ContextManager(SYSTEM_PROMPT),
            max_steps=cfg.fixer_max_steps,
            budget=BudgetTracker(max_tokens=remaining,
                                 max_wall_seconds=cfg.budget_wall_seconds),
            loop_detect_window=cfg.loop_detect_window,
            tool_result_max_chars=cfg.tool_result_max_chars,
            model_name=cfg.fixer.model,
            price_map=cfg.price_map,
        )
        messages = build_initial_messages(failure, diagnosis)

        for _ in range(cfg.fix_guard_retries + 1):
            outcome = await consume(loop.run(messages=list(messages)))
            tokens += outcome.tokens
            cost += outcome.cost_usd
            lines = await _diff_lines(sandbox)

            if lines == 0:
                guard_hits.append("empty_diff")
                abort_reason = "empty_diff"
                feedback = _EMPTY_FEEDBACK
            elif lines > cfg.max_diff_lines:
                guard_hits.append("huge_diff")
                abort_reason = "huge_diff"
                feedback = _HUGE_FEEDBACK.format(
                    lines=lines, limit=cfg.max_diff_lines)
                await _rollback(sandbox)
                touched.clear()
                lines = 0        # 已回滚，工作区确实没有改动了
            else:
                abort_reason = None
                break

            messages = messages + [
                Message(role=Role.ASSISTANT, content=outcome.text or "（无输出）"),
                Message(role=Role.USER, content=feedback),
            ]
    finally:
        await sandbox.close()

    return {
        "spent_tokens": state["spent_tokens"] + tokens,
        "spent_usd": state["spent_usd"] + cost,
        "touched": sorted(touched),
        "guard_hits": guard_hits,
        "diff_lines": lines,
        "abort_reason": abort_reason,
        "abort": None,
    }
```

- [ ] **步骤 5：给 `AifixState` 加字段**

在 `src/aifix/graph.py` 的 `AifixState` 中，`verdict` 之后插入：

```python
    touched: list[str]
    guard_hits: list[str]
    diff_lines: int
    abort_reason: str | None
```

并在 `new_state` 的返回值中补上初值：

```python
        touched=[], guard_hits=[], diff_lines=0, abort_reason=None,
```

- [ ] **步骤 6：运行测试验证通过**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q
```

预期：4 passed。

- [ ] **步骤 7：让 `verify_node` 使用 `touched`**

把 `src/aifix/nodes/verify.py` 中的

```python
        wt.commit(f"fix: {target}")
```

改为

```python
        wt.commit(f"fix: {target}", paths=state.get("touched") or [])
```

- [ ] **步骤 8：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 9：Commit**

```bash
git add src/aifix/nodes/fix.py src/aifix/nodes/verify.py \
        src/aifix/config.py src/aifix/graph.py tests/test_nodes_fix_guards.py
git commit -m "feat(nodes): 空 diff 与巨型 diff 守卫

空 diff 是本领域最常见也最隐蔽的失败 —— 模型自信宣布完成却一字未改。
巨型 diff 说明它放弃理解、直接重写整个文件，那种补丁即使测试转绿也不该合。

两条都以「带反馈重试」处理而非直接失败：模型拿到具体问题描述后通常
下一次就对了。守卫重试不计入 attempt —— attempt 衡量的是修复尝试，
而这里连一次有效尝试都没产生。巨型 diff 触发时先回滚，否则下一轮
会在垃圾上继续改。"
```

---

### 任务 5：flaky 过滤

三态判定有个天然弱点：不稳定的测试会污染判定。偶然转红会把一个本来正确的补丁回滚掉。

**文件：**
- 修改：`src/aifix/nodes/verify.py`、`src/aifix/config.py`
- 测试：`tests/test_flaky.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_flaky.py`：

```python
from aifix.adapters.base import Failure, FailureSet
from aifix.nodes.verify import filter_flaky


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


async def test_no_new_failures_skips_rerun():
    """没有回归嫌疑就不重跑 —— 这是成本控制的关键。"""
    calls = []

    async def _rerun(ids):
        calls.append(ids)
        return _fs()

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a"), rerun=_rerun)
    assert confirmed == set()
    assert flaky == set()
    assert calls == [], "无新失败时不该触发重跑"


async def test_rerun_green_marks_flaky():
    """重跑就绿 → 判为抖动，不算回归。"""
    async def _rerun(ids):
        return _fs()          # 重跑全绿

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b"), rerun=_rerun)
    assert confirmed == set()
    assert flaky == {"b"}


async def test_rerun_still_red_confirms_regression():
    async def _rerun(ids):
        return _fs("b")       # 重跑还是红

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b"), rerun=_rerun)
    assert confirmed == {"b"}
    assert flaky == set()


async def test_only_new_failures_are_rerun():
    """只重跑新增的那几个，不是全量 —— 成本近似为零。"""
    seen = []

    async def _rerun(ids):
        seen.append(set(ids))
        return _fs("c")

    confirmed, flaky = await filter_flaky(
        baseline=_fs("a"), current=_fs("a", "b", "c"), rerun=_rerun)
    assert seen == [{"b", "c"}]
    assert confirmed == {"c"}
    assert flaky == {"b"}
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_flaky.py -q
```

预期：FAIL，`ImportError: cannot import name 'filter_flaky'`。

- [ ] **步骤 3：编写实现**

在 `src/aifix/nodes/verify.py` 的 `_worktree` 之后插入：

```python
async def filter_flaky(baseline: FailureSet, current: FailureSet,
                       rerun) -> tuple[set[str], set[str]]:
    """把「新增失败」拆成确认回归与抖动两部分。

    只在出现新失败时触发重跑，且只重跑那几个用例 —— 成本近似为零，
    却能挡掉绝大部分因抖动导致的误回滚（把一个本来正确的补丁滚掉，
    是这个系统最昂贵的错误）。

    rerun：async callable，接收 test_id 集合，返回重跑后的 FailureSet。
    返回 (确认回归, 判为抖动)。
    """
    new = current.ids - baseline.ids
    if not new:
        return set(), set()
    again = await rerun(sorted(new))
    confirmed = new & again.ids
    return confirmed, new - confirmed
```

并在文件顶部把 `FailureSet` 的导入确认存在（M1 已导入）。

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_flaky.py -q
```

预期：4 passed。

- [ ] **步骤 5：把 flaky 过滤接进 `verify_node`**

把 `src/aifix/nodes/verify.py` 的 `verify_node` 替换为：

```python
async def verify_node(state: AifixState) -> dict[str, Any]:
    """跑全量、过滤抖动、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapter = adapter_for(state["adapter_name"])
    worktree_path = Path(state["worktree_path"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    current = await run_full_suite(worktree_path, adapter)

    async def _rerun(ids: list[str]) -> FailureSet:
        return await run_scoped(worktree_path, adapter, ids)

    confirmed, flaky = await filter_flaky(baseline, current, _rerun)
    # 抖动的用例从当前结果里剔除，避免它们把判定拖成 WORSE
    effective = FailureSet({k: v for k, v in current.failures.items()
                            if k not in flaky})
    verdict = compare(baseline, effective, target)

    results = list(state["results"])
    common = {"flaky_filtered": sorted(flaky),
              "confirmed_regressions": sorted(confirmed)}

    if verdict is Verdict.BETTER:
        wt.commit(f"fix: {target}", paths=state.get("touched") or [])
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"], "abort_reason": None})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": 0, **common}

    wt.rollback()
    if state["attempt"] >= cfg.max_attempts:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"],
                        "abort_reason": state.get("abort_reason") or "max_attempts"})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": state.get("consecutive_failures", 0) + 1,
                **common}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None, **common}
```

- [ ] **步骤 6：在 `baseline.py` 增加 `run_scoped`**

在 `src/aifix/nodes/baseline.py` 的 `run_full_suite` 之后插入：

```python
async def run_scoped(worktree: Path, adapter: PytestAdapter,
                     test_ids: list[str], timeout: float = 300.0):
    """只跑指定用例并解析报告。供 flaky 确认使用 —— 成本远低于全量。"""
    report = ".aifix-recheck.xml"
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.scoped_test_command(test_ids, report), timeout)
        return parse_junit([worktree / report], adapter.make_test_id)
    finally:
        await sb.exec(["rm", "-f", report], 10.0)
        await sb.close()
```

- [ ] **步骤 7：给 `AifixState` 加字段**

在 `src/aifix/graph.py` 的 `AifixState` 中补：

```python
    flaky_filtered: list[str]
    confirmed_regressions: list[str]
    consecutive_failures: int
```

并在 `new_state` 补初值：

```python
        flaky_filtered=[], confirmed_regressions=[], consecutive_failures=0,
```

- [ ] **步骤 8：在 `verify.py` 顶部补导入**

```python
from .baseline import adapter_for, run_full_suite, run_scoped
```

- [ ] **步骤 9：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 10：Commit**

```bash
git add src/aifix/nodes/verify.py src/aifix/nodes/baseline.py \
        src/aifix/graph.py tests/test_flaky.py
git commit -m "feat(verify): flaky 过滤

三态判定的天然弱点是不稳定测试会污染判定 —— 偶然转红会把一个
本来正确的补丁回滚掉，这是这个系统最昂贵的错误。

只在出现新失败时触发重跑，且只重跑那几个用例，成本近似为零。
被判为抖动的用例记进 trace —— 顺带产出一份目标项目的 flaky 清单。"
```

---

### 任务 6：连续失败熔断

连着几个 failure 一个都没修好，大概率不是「这些 bug 恰好都难」，而是环境坏了、prompt 崩了、或今天这个模型不行。继续跑只是匀速烧钱。

**文件：**
- 修改：`src/aifix/graph.py`
- 测试：`tests/test_circuit_breaker.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_circuit_breaker.py`：

```python
from aifix.config import AifixConfig
from aifix.graph import check_circuit_breaker, route_after_verify


def _st(consecutive: int, limit: int = 3, **over):
    return {"consecutive_failures": consecutive,
            "config": AifixConfig(consecutive_failure_limit=limit),
            "abort": None, "current": None, "queue": ["x"], **over}


def test_below_limit_does_not_trip():
    assert check_circuit_breaker(_st(2)) is None


def test_at_limit_trips():
    msg = check_circuit_breaker(_st(3))
    assert msg is not None
    assert "连续 3" in msg


def test_above_limit_trips():
    assert check_circuit_breaker(_st(5)) is not None


def test_success_resets_counter():
    """verify 判 BETTER 时把计数清零 —— 熔断看的是连续，不是累计。"""
    assert check_circuit_breaker(_st(0)) is None


def test_route_after_verify_goes_to_report_when_tripped():
    st = _st(3)
    assert route_after_verify(st) == "report"


def test_route_after_verify_continues_when_not_tripped():
    st = _st(1)
    assert route_after_verify(st) == "detect"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_circuit_breaker.py -q
```

预期：FAIL，`ImportError: cannot import name 'check_circuit_breaker'`。

- [ ] **步骤 3：编写实现**

在 `src/aifix/graph.py` 的 `route_after_baseline` 之前插入：

```python
def check_circuit_breaker(state: AifixState) -> str | None:
    """连续失败达到阈值就中止整个 run，返回中止原因；未达阈值返回 None。

    连着几个 failure 一个都没修好，大概率不是「这些 bug 恰好都难」，
    而是环境坏了、prompt 崩了、或今天这个模型不行。继续跑只是匀速烧钱。
    比预算上限更早生效，也更有信息量 —— 它把「钱花完了」变成
    「出问题了，去看 trace」。
    """
    limit = state["config"].consecutive_failure_limit
    n = state.get("consecutive_failures", 0)
    if n >= limit:
        return f"连续 {n} 个 failure 均未修复，疑似系统性问题，已中止"
    return None
```

并把 `route_after_verify` 替换为：

```python
def route_after_verify(state: AifixState) -> str:
    """current 仍在 → 同一个 failure 重试；已清空 → 取下一个或收尾。"""
    if state.get("abort") or check_circuit_breaker(state):
        return "report"
    if state["current"] is not None:
        return "detect"
    return "detect" if state["queue"] else "report"
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_circuit_breaker.py -q
```

预期：6 passed。

- [ ] **步骤 5：让 `cli.run_once` 写入中止原因**

在 `src/aifix/cli.py` 的主循环中，把

```python
            state.update(await verify_node(state))
```

改为

```python
            state.update(await verify_node(state))
            tripped = check_circuit_breaker(state)
            if tripped:
                state["abort"] = tripped
                break
```

并在文件顶部的导入中加入 `check_circuit_breaker`：

```python
from .graph import AifixState, check_circuit_breaker, new_state
```

- [ ] **步骤 6：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/graph.py src/aifix/cli.py tests/test_circuit_breaker.py
git commit -m "feat(graph): 连续失败熔断

连着几个 failure 一个都没修好，大概率是环境坏了 / prompt 崩了 /
今天这个模型不行，继续跑只是匀速烧钱。比预算上限更早生效，
也更有信息量 —— 把「钱花完了」变成「出问题了，去看 trace」。"
```

---

# 阶段 3：预算

### 任务 7：三层预算动态分配

**文件：**
- 创建：`src/aifix/budget.py`
- 测试：`tests/test_budget.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_budget.py`：

```python
import pytest

from aifix.budget import RunBudget


def test_allocates_by_remaining_failures():
    """动态分配：前面省下的额度自动流给后面难的。"""
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    assert b.for_failure(remaining_failures=4) == 25_000
    b.charge(tokens=5_000, usd=0.1)
    # 剩 95k，还剩 3 个 → 每个 31666
    assert b.for_failure(remaining_failures=3) == 31_666


def test_fixed_split_would_waste_but_dynamic_does_not():
    """固定切分会让最后一个 failure 明明有钱却因自己那份用完而放弃。"""
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    b.charge(tokens=1_000, usd=0.01)     # 第一个只花了一点
    assert b.for_failure(remaining_failures=1) == 99_000


def test_never_returns_below_floor():
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    b.charge(tokens=99_999, usd=0.0)
    assert b.for_failure(remaining_failures=1) == RunBudget.FLOOR_TOKENS


def test_zero_remaining_failures_returns_all():
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    assert b.for_failure(remaining_failures=0) == 100_000


def test_exhausted_on_tokens():
    b = RunBudget(total_tokens=1_000, total_usd=2.0, total_seconds=600)
    assert b.exhausted() is None
    b.charge(tokens=1_000, usd=0.0)
    assert "token" in b.exhausted()


def test_exhausted_on_usd():
    b = RunBudget(total_tokens=100_000, total_usd=0.5, total_seconds=600)
    b.charge(tokens=10, usd=0.5)
    assert "美元" in b.exhausted()


def test_exhausted_on_wall_clock():
    clock = iter([0.0, 0.0, 700.0])
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600,
                  clock=lambda: next(clock))
    b.start()
    assert b.exhausted() is None
    assert "时间" in b.exhausted()


def test_spent_accessors():
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    b.charge(tokens=1_200, usd=0.05)
    b.charge(tokens=800, usd=0.03)
    assert b.spent_tokens == 2_000
    assert abs(b.spent_usd - 0.08) < 1e-9
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_budget.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.budget'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/budget.py`：

```python
"""三层预算：全局 → 单 failure → 单次 AgentLoop。

动态分配而非固定切分：前面省下来的额度自动流给后面难的。固定切分会
出现「最后一个 failure 明明有钱，却因为自己那份用完了而放弃」。
"""
from __future__ import annotations

import time
from typing import Callable


class RunBudget:
    FLOOR_TOKENS = 10_000        # 再紧也要给一次有意义尝试的余地

    def __init__(self, total_tokens: int, total_usd: float,
                 total_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._total_tokens = total_tokens
        self._total_usd = total_usd
        self._total_seconds = total_seconds
        self._clock = clock
        self._start: float | None = None
        self.spent_tokens = 0
        self.spent_usd = 0.0

    def start(self) -> None:
        if self._start is None:
            self._start = self._clock()

    def charge(self, tokens: int, usd: float) -> None:
        self.spent_tokens += tokens
        self.spent_usd += usd

    def remaining_tokens(self) -> int:
        return max(self._total_tokens - self.spent_tokens, 0)

    def for_failure(self, remaining_failures: int) -> int:
        """分给下一个 failure 的 token 额度。"""
        left = self.remaining_tokens()
        if remaining_failures <= 0:
            return left
        return max(left // remaining_failures, self.FLOOR_TOKENS)

    def exhausted(self) -> str | None:
        """超限返回原因，未超返回 None。"""
        if self.spent_tokens >= self._total_tokens:
            return f"token 预算耗尽：{self.spent_tokens} / {self._total_tokens}"
        if self.spent_usd >= self._total_usd:
            return f"美元预算耗尽：${self.spent_usd:.2f} / ${self._total_usd:.2f}"
        if self._start is not None:
            elapsed = self._clock() - self._start
            if elapsed >= self._total_seconds:
                return f"时间预算耗尽：{elapsed:.0f}s / {self._total_seconds:.0f}s"
        return None
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_budget.py -q
```

预期：8 passed。

- [ ] **步骤 5：把预算接进 `cli.run_once`**

在 `src/aifix/cli.py` 顶部导入：

```python
from .budget import RunBudget
```

把 `run_once` 的主循环替换为：

```python
        budget = RunBudget(total_tokens=config.budget_tokens,
                           total_usd=config.budget_usd,
                           total_seconds=config.budget_wall_seconds)
        budget.start()

        while True:
            if state["current"] is None:
                if state["abort"] or not state["queue"]:
                    break
                state["current"] = state["queue"].pop(0)
                state["attempt"] = 1
            spent = budget.exhausted()
            if spent:
                state["abort"] = spent
                break
            # 剩余 failure 数 = 队列里的 + 手上这个
            state["failure_token_budget"] = budget.for_failure(
                len(state["queue"]) + 1)
            before = state["spent_tokens"], state["spent_usd"]
            state.update(await detect_node(state, client=detector_client))
            state.update(await fix_node(state, client=fixer_client))
            budget.charge(state["spent_tokens"] - before[0],
                          state["spent_usd"] - before[1])
            state.update(await verify_node(state))
            tripped = check_circuit_breaker(state)
            if tripped:
                state["abort"] = tripped
                break
```

- [ ] **步骤 6：让 `fix_node` 使用分配额度**

把 `src/aifix/nodes/fix.py` 中的

```python
    remaining = max(cfg.budget_tokens - state["spent_tokens"], 10_000)
```

改为

```python
    # 优先用本轮分配到的额度；未分配（如单测直接调用）时退回全局剩余
    remaining = state.get("failure_token_budget") or max(
        cfg.budget_tokens - state["spent_tokens"], 10_000)
```

- [ ] **步骤 7：给 `AifixState` 加字段**

在 `src/aifix/graph.py` 的 `AifixState` 中补 `failure_token_budget: int`，并在 `new_state` 补 `failure_token_budget=0,`。

- [ ] **步骤 8：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 9：Commit**

```bash
git add src/aifix/budget.py src/aifix/cli.py src/aifix/nodes/fix.py \
        src/aifix/graph.py tests/test_budget.py
git commit -m "feat(budget): 三层预算动态分配

全局 → 单 failure（全局剩余 ÷ 剩余 failure 数）→ 单次 AgentLoop。
动态分配而非固定切分：前面省下的额度自动流给后面难的，避免
「最后一个 failure 明明有钱却因自己那份用完而放弃」。

三个维度都会触发中止：token、美元、墙钟。"
```

---

# 阶段 4：可观测性

### 任务 8：trace 模块

**文件：**
- 创建：`src/aifix/trace.py`
- 测试：`tests/test_trace.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_trace.py`：

```python
import json

from harness.events import RunStarted, TextDelta

from aifix.trace import RunTrace


def test_events_written_as_jsonl(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.record_events([RunStarted(run_id="r1"), TextDelta(text="你好")])
    t.close()
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["type"] == "RunStarted"
    assert first["data"]["run_id"] == "r1"


def test_records_domain_facts(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.fact("verdict", "better", failure="a", attempt=1)
    t.close()
    lines = (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert rec["key"] == "verdict"
    assert rec["value"] == "better"
    assert rec["failure"] == "a"
    assert rec["attempt"] == 1


def test_spans_nest_without_provider(tmp_path):
    """没配 OTel provider 时是 no-op tracer，不该报错。"""
    t = RunTrace(tmp_path, run_id="r1")
    with t.run_span():
        with t.failure_span("tests/x.py::test_y"):
            with t.attempt_span(1):
                t.fact("verdict", "same")
    t.close()
    assert (tmp_path / "facts.jsonl").exists()


def test_close_is_idempotent(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.close()
    t.close()


def test_creates_directory(tmp_path):
    d = tmp_path / "deep" / "nested"
    t = RunTrace(d, run_id="r1")
    t.fact("x", 1)
    t.close()
    assert (d / "facts.jsonl").is_file()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_trace.py -q
```

预期：FAIL，`ModuleNotFoundError: No module named 'aifix.trace'`。

- [ ] **步骤 3：编写实现**

创建 `src/aifix/trace.py`：

```python
"""三层嵌套 trace：aifix.run → aifix.failure → aifix.attempt。

框架的 span（run / step / model_call / tool_call:*）会自动挂在这三层
下面 —— OpenTelemetry 的 span 是天然嵌套的，app 层只要在对的位置
开 span，不需要打通任何东西。

事实（facts）与事件（events）分开落盘：events.jsonl 是模型每一步看到
什么、决定做什么的原始素材（回放用）；facts.jsonl 是领域判断的结论
（verdict / rollback / flaky…），也是评测直接取用的数据源。
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Iterable

from harness.persistence.serialize import event_to_dict
from harness.telemetry.tracer import get_tracer
from opentelemetry import trace as otel_trace


class RunTrace:
    def __init__(self, out_dir: Path, run_id: str) -> None:
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._tracer = get_tracer("aifix")
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._facts = (self.dir / "facts.jsonl").open("a", encoding="utf-8")
        self._current: dict[str, Any] = {}
        self._closed = False

    # ---------- span ----------

    @contextlib.contextmanager
    def run_span(self):
        with self._tracer.start_as_current_span("aifix.run") as s:
            s.set_attribute("aifix.run_id", self.run_id)
            yield s

    @contextlib.contextmanager
    def failure_span(self, test_id: str):
        self._current["failure"] = test_id
        with self._tracer.start_as_current_span("aifix.failure") as s:
            s.set_attribute("aifix.test_id", test_id)
            try:
                yield s
            finally:
                self._current.pop("failure", None)

    @contextlib.contextmanager
    def attempt_span(self, attempt: int):
        self._current["attempt"] = attempt
        with self._tracer.start_as_current_span("aifix.attempt") as s:
            s.set_attribute("aifix.attempt", attempt)
            try:
                yield s
            finally:
                self._current.pop("attempt", None)

    # ---------- 落盘 ----------

    def fact(self, key: str, value: Any, **extra: Any) -> None:
        """记一条领域事实，并同时打到当前 span 的属性上。"""
        rec = {"run_id": self.run_id, "key": key, "value": value,
               **self._current, **extra}
        self._facts.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._facts.flush()
        # 同一条事实也打到当前 span 上：没配 provider 时是 no-op，零开销
        cur = otel_trace.get_current_span()
        cur.set_attribute(
            f"aifix.{key}",
            value if isinstance(value, (str, int, float, bool))
            else json.dumps(value, ensure_ascii=False))

    def record_events(self, events: Iterable[Any]) -> None:
        """把 AgentLoop 的事件流落成 jsonl，供 replay 使用。"""
        for ev in events:
            self._events.write(
                json.dumps(event_to_dict(ev), ensure_ascii=False) + "\n")
        self._events.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._events.close()
        self._facts.close()
        self._closed = True
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_trace.py -q
```

预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/trace.py tests/test_trace.py
git commit -m "feat(trace): 三层嵌套 span 与双通道落盘

aifix.run → aifix.failure → aifix.attempt；框架的 span 自动挂在下面。

events.jsonl 是回放素材（模型每步看到什么、决定做什么），
facts.jsonl 是领域判断结论（verdict / rollback / flaky…），
后者同时是评测的数据源 —— 不必为评测单独埋点。"
```

---

### 任务 9：把 trace 接进各节点

**文件：**
- 修改：`src/aifix/nodes/{detect,fix,verify}.py`、`src/aifix/cli.py`、`src/aifix/graph.py`
- 测试：`tests/test_trace_wiring.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_trace_wiring.py`：

```python
import json

from harness.llm.base import StreamChunk
from harness.usage import Usage

from aifix.adapters.base import Failure, SourceCandidate
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.detect import detect_node
from aifix.nodes.preflight import preflight_node
from aifix.trace import RunTrace

_TID = "tests/test_calc.py::test_add"
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


async def test_detect_records_locate_hit(buggy_repo, tmp_path):
    """suspect_file 是否命中 locate_source 候选 —— 评测直接取这条。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = new_state(buggy_repo, AifixConfig(), run_id="r1")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["current"] = _TID
        st["attempt"] = 1
        st["_failures"] = {_TID: Failure(
            test_id=_TID, classname="c", name="n", message="m",
            trace=f'File "{wt.path}/calc.py", line 2, in add\n')}
        trace = RunTrace(tmp_path, run_id="r1")
        st["_trace"] = trace
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    facts = [json.loads(x) for x in
             (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    hit = [f for f in facts if f["key"] == "locate_hit"]
    assert hit and hit[0]["value"] is True


async def test_detect_records_miss(buggy_repo, tmp_path):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = new_state(buggy_repo, AifixConfig(), run_id="r1")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["current"] = _TID
        st["attempt"] = 1
        st["_failures"] = {_TID: Failure(
            test_id=_TID, classname="c", name="n", message="m", trace="")}
        trace = RunTrace(tmp_path, run_id="r1")
        st["_trace"] = trace
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    facts = [json.loads(x) for x in
             (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    hit = [f for f in facts if f["key"] == "locate_hit"]
    assert hit and hit[0]["value"] is False


async def test_detect_without_trace_still_works(buggy_repo):
    """trace 缺席不该影响主流程（单测直接调节点时没有 trace）。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = new_state(buggy_repo, AifixConfig(), run_id="r1")
        st.update(preflight_node(st))
        st["worktree_path"] = str(wt.path)
        st["current"] = _TID
        st["attempt"] = 1
        st["_failures"] = {_TID: Failure(
            test_id=_TID, classname="c", name="n", message="m", trace="")}
        out = await detect_node(st, client=_Scripted([_text(_DIAG)]))
        assert out["diagnosis"]["suspect_file"] == "calc.py"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_trace_wiring.py -q
```

预期：前两个 FAIL（`facts.jsonl` 里没有 `locate_hit`），第三个 PASS。

- [ ] **步骤 3：在 `graph.py` 加 `_trace` 字段与取值辅助**

在 `AifixState` 中补：

```python
    _trace: Any
```

并在 `graph.py` 末尾追加：

```python
def trace_of(state: AifixState):
    """取当前 run 的 trace；未接线时返回一个吞掉所有调用的空实现。"""
    t = state.get("_trace")
    if t is not None:
        return t
    return _NullTrace()


class _NullTrace:
    """trace 缺席时的空实现 —— 单测直接调节点时不必构造 trace。"""

    def fact(self, *a: Any, **k: Any) -> None: ...

    def record_events(self, *a: Any, **k: Any) -> None: ...
```

- [ ] **步骤 4：在 `detect_node` 记录 `locate_hit`**

把 `src/aifix/nodes/detect.py` 的 `detect_node` 尾部替换为：

```python
    diagnosis = parse_diagnosis(outcome.text) if outcome.ok else None
    trace = trace_of(state)
    trace.record_events(outcome.events)
    if diagnosis is not None:
        hit = any(c.path == diagnosis.suspect_file for c in candidates)
        trace.fact("locate_hit", hit)
        trace.fact("suspect_file", diagnosis.suspect_file)
    else:
        trace.fact("locate_hit", False)
        trace.fact("diagnosis_parse_failed", True)
    return {
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "spent_tokens": state["spent_tokens"] + outcome.tokens,
        "spent_usd": state["spent_usd"] + outcome.cost_usd,
    }
```

并在顶部导入中加入：

```python
from ..graph import AifixState, trace_of
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/test_trace_wiring.py -q
```

预期：3 passed。

- [ ] **步骤 6：在 `fix_node` 记录守卫与 diff 规模**

在 `src/aifix/nodes/fix.py` 的 `return` 之前插入：

```python
    trace = trace_of(state)
    trace.record_events(outcome.events)
    trace.fact("diff_lines", lines)
    trace.fact("touched", sorted(touched))
    for hit in guard_hits:
        trace.fact("guard_hit", hit)
```

并在顶部导入中把 `from ..graph import AifixState` 改为：

```python
from ..graph import AifixState, trace_of
```

- [ ] **步骤 7：在 `verify_node` 记录判定与回滚**

在 `src/aifix/nodes/verify.py` 的 `verdict = compare(...)` 之后插入：

```python
    trace = trace_of(state)
    trace.fact("verdict", verdict.value)
    if flaky:
        trace.fact("flaky_filtered", sorted(flaky))
    if confirmed:
        trace.fact("confirmed_regressions", sorted(confirmed))
    if verdict is not Verdict.BETTER:
        trace.fact("rollback", True)
```

并在顶部导入中把 `from ..graph import AifixState` 改为：

```python
from ..graph import AifixState, trace_of
```

- [ ] **步骤 8：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 9：Commit**

```bash
git add src/aifix/nodes/detect.py src/aifix/nodes/fix.py \
        src/aifix/nodes/verify.py src/aifix/graph.py tests/test_trace_wiring.py
git commit -m "feat(trace): 领域事实接进 detect / fix / verify

locate_hit 让 trace 同时成为评测的数据源，不必为评测单独埋点。
trace 缺席时走空实现 —— 单测直接调节点不必构造 trace。"
```

---

### 任务 10：产物落盘与报告

**文件：**
- 修改：`src/aifix/nodes/report.py`、`src/aifix/cli.py`
- 测试：`tests/test_artifacts.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_artifacts.py`：

```python
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


async def test_artifacts_written(buggy_repo):
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="art1",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    d = buggy_repo / ".aifix" / "runs" / "art1"
    assert (d / "report.md").is_file()
    assert (d / "facts.jsonl").is_file()
    assert (d / "events.jsonl").is_file()
    assert "1 / 1" in (d / "report.md").read_text(encoding="utf-8")
    assert state["results"][0]["verdict"] == "better"


async def test_facts_contain_verdict_and_locate_hit(buggy_repo):
    await run_once(
        buggy_repo, AifixConfig(), run_id="art2",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    d = buggy_repo / ".aifix" / "runs" / "art2"
    keys = {json.loads(x)["key"] for x in
            (d / "facts.jsonl").read_text(encoding="utf-8").splitlines()}
    assert "verdict" in keys
    assert "locate_hit" in keys
    assert "diff_lines" in keys


async def test_delivery_branch_has_only_source(buggy_repo):
    """交付分支不该有构建产物 —— 任务 1-3 的最终验收。"""
    await run_once(
        buggy_repo, AifixConfig(), run_id="art3",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    files = subprocess.run(
        ["git", "diff", "--name-only", "main..aifix/art3"],
        cwd=buggy_repo, capture_output=True, text=True).stdout.split()
    assert files == ["calc.py"], f"交付分支混入了非源码文件：{files}"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_artifacts.py -q
```

预期：FAIL，`.aifix/runs/art1/report.md` 不存在。

- [ ] **步骤 3：让 `report_node` 写盘**

把 `src/aifix/nodes/report.py` 的 `report_node` 替换为：

```python
def report_node(state: dict[str, Any]) -> dict[str, Any]:
    md = render_report(state)
    out = state.get("artifact_dir")
    if out:
        from pathlib import Path
        p = Path(out)
        p.mkdir(parents=True, exist_ok=True)
        (p / "report.md").write_text(md, encoding="utf-8")
    return {"report_md": md}
```

- [ ] **步骤 4：在 `cli.run_once` 接线 trace 与产物目录**

把 `src/aifix/cli.py` 的 `run_once` 替换为：

```python
async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> AifixState:
    """按状态图的语义顺序执行一次完整 run。"""
    state = new_state(repo, config, run_id=run_id)
    state.update(preflight_node(state))
    if state["abort"]:
        state["report_md"] = render_report(state)
        return state

    artifact_dir = Path(repo) / ".aifix" / "runs" / run_id
    state["artifact_dir"] = str(artifact_dir)
    trace = RunTrace(artifact_dir, run_id=run_id)
    state["_trace"] = trace

    try:
        with Worktree(repo, run_id=run_id) as wt, trace.run_span():
            state["worktree_path"] = str(wt.path)
            state["branch"] = wt.branch

            state.update(await baseline_node(state))
            trace.fact("baseline_failures", len(state["baseline_ids"]))

            budget = RunBudget(total_tokens=config.budget_tokens,
                               total_usd=config.budget_usd,
                               total_seconds=config.budget_wall_seconds)
            budget.start()

            while True:
                if state["current"] is None:
                    if state["abort"] or not state["queue"]:
                        break
                    state["current"] = state["queue"].pop(0)
                    state["attempt"] = 1
                spent = budget.exhausted()
                if spent:
                    state["abort"] = spent
                    trace.fact("abort", spent)
                    break
                state["failure_token_budget"] = budget.for_failure(
                    len(state["queue"]) + 1)
                before = state["spent_tokens"], state["spent_usd"]
                with trace.failure_span(state["current"]), \
                        trace.attempt_span(state["attempt"]):
                    state.update(await detect_node(state, client=detector_client))
                    state.update(await fix_node(state, client=fixer_client))
                    budget.charge(state["spent_tokens"] - before[0],
                                  state["spent_usd"] - before[1])
                    state.update(await verify_node(state))
                tripped = check_circuit_breaker(state)
                if tripped:
                    state["abort"] = tripped
                    trace.fact("abort", tripped)
                    break

        state.update(report_node(state))
    finally:
        trace.close()
    return state
```

并把 `cli.py` 顶部导入替换为：

```python
from .budget import RunBudget
from .config import AifixConfig
from .delivery import Worktree
from .graph import AifixState, check_circuit_breaker, new_state
from .nodes.baseline import baseline_node
from .nodes.detect import detect_node
from .nodes.fix import fix_node
from .nodes.preflight import preflight_node
from .nodes.report import render_report, report_node
from .nodes.verify import verify_node
from .trace import RunTrace
```

- [ ] **步骤 5：给 `AifixState` 加 `artifact_dir`**

在 `src/aifix/graph.py` 的 `AifixState` 中补 `artifact_dir: str`，`new_state` 中补 `artifact_dir="",`。

- [ ] **步骤 6：运行测试验证通过**

```bash
uv run pytest tests/test_artifacts.py -q
```

预期：3 passed。

- [ ] **步骤 7：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 8：Commit**

```bash
git add src/aifix/cli.py src/aifix/nodes/report.py src/aifix/graph.py \
        tests/test_artifacts.py
git commit -m "feat(trace): 产物落盘到 .aifix/runs/<run_id>/

report.md（人看）+ facts.jsonl（领域结论，评测取用）+
events.jsonl（回放素材）。三层 span 在 run_once 里接线：
run → failure → attempt，框架的 span 自动挂在下面。

test_delivery_branch_has_only_source 是任务 1-3 的最终验收：
交付分支只有源码，没有任何构建产物。"
```

---

# 阶段 5：断点续跑

### 任务 11：`SqliteSaver` 接入

`build_graph()` 在 M1 就写好了但一直没被用上。接上 checkpointer 后，跑到一半崩掉能从上一个节点边界续跑。

**文件：**
- 修改：`src/aifix/graph.py`、`src/aifix/cli.py`、`src/aifix/config.py`
- 测试：`tests/test_checkpoint.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_checkpoint.py`：

```python
from aifix.config import AifixConfig
from aifix.graph import build_graph, checkpointer_for


def test_checkpointer_disabled_by_default(tmp_path):
    assert checkpointer_for(AifixConfig(), tmp_path) is None


def test_checkpointer_created_when_enabled(tmp_path):
    cp = checkpointer_for(AifixConfig(enable_checkpoint=True), tmp_path)
    assert cp is not None
    assert (tmp_path / "checkpoint.sqlite").exists()


def test_graph_compiles_with_checkpointer(tmp_path):
    cp = checkpointer_for(AifixConfig(enable_checkpoint=True), tmp_path)
    g = build_graph(checkpointer=cp)
    names = {n for n in g.get_graph().nodes if not n.startswith("__")}
    assert names == {"preflight", "baseline", "take_next",
                     "detect", "fix", "verify", "report"}


def test_graph_compiles_without_checkpointer():
    g = build_graph()
    assert g is not None
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_checkpoint.py -q
```

预期：FAIL，`ImportError: cannot import name 'checkpointer_for'`。

- [ ] **步骤 3：给 `AifixConfig` 加开关**

在 `src/aifix/config.py` 的 `allow_test_edits` 之前插入：

```python
    # 断点续跑：跑到一半崩掉能从上一个节点边界继续。默认关 ——
    # 它会在产物目录下留一个 sqlite 文件，按需开启。
    enable_checkpoint: bool = False
```

- [ ] **步骤 4：编写实现**

在 `src/aifix/graph.py` 末尾追加：

```python
def checkpointer_for(config: AifixConfig, artifact_dir: Path):
    """按配置建 LangGraph 的 SqliteSaver；未开启返回 None。

    SqliteSaver 在独立包 langgraph-checkpoint-sqlite 里，
    langgraph 本体只依赖抽象基座 langgraph-checkpoint。
    """
    if not config.enable_checkpoint:
        return None
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    d = Path(artifact_dir)
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / "checkpoint.sqlite"), check_same_thread=False)
    return SqliteSaver(conn)
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/test_checkpoint.py -q
```

预期：4 passed。

- [ ] **步骤 6：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/graph.py src/aifix/config.py tests/test_checkpoint.py
git commit -m "feat(graph): SqliteSaver 接入

build_graph() 在 M1 就写好但一直没被用上。默认关闭 —— 它会在产物
目录下留一个 sqlite 文件，按需开启。

SqliteSaver 在独立包 langgraph-checkpoint-sqlite 里，
langgraph 本体只依赖抽象基座。"
```

---

### 任务 12：CLI 暴露新能力

**文件：**
- 修改：`src/aifix/cli.py`
- 测试：`tests/test_cli_args.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_cli_args.py`：

```python
import pytest

from aifix.cli import build_parser


def test_run_accepts_repo():
    args = build_parser().parse_args(["run", "/tmp/x"])
    assert args.repo == "/tmp/x"
    assert args.cmd == "run"


def test_run_repo_defaults_to_cwd():
    assert build_parser().parse_args(["run"]).repo == "."


def test_budget_override():
    args = build_parser().parse_args(["run", "--budget", "0.5"])
    assert args.budget == 0.5


def test_test_filter():
    args = build_parser().parse_args(["run", "--test", "tests/x.py::y"])
    assert args.test == "tests/x.py::y"


def test_dry_run_flag():
    assert build_parser().parse_args(["run", "--dry-run"]).dry_run is True
    assert build_parser().parse_args(["run"]).dry_run is False


def test_unknown_subcommand_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nope"])
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_cli_args.py -q
```

预期：FAIL，`ImportError: cannot import name 'build_parser'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/cli.py` 的 `main` 替换为：

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aifix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="修复当前 repo 的失败测试")
    run.add_argument("repo", nargs="?", default=".")
    run.add_argument("--test", default=None,
                     help="只修这一个失败用例（test_id）")
    run.add_argument("--budget", type=float, default=None,
                     help="本次 run 的美元预算上限")
    run.add_argument("--dry-run", action="store_true",
                     help="只跑 preflight + baseline，报告有多少活")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd != "run":
        return
    config = AifixConfig()
    if args.budget is not None:
        config = config.model_copy(update={"budget_usd": args.budget})
    state = asyncio.run(run_once(
        Path(args.repo).resolve(), config, run_id=uuid.uuid4().hex[:8],
        only_test=args.test, dry_run=args.dry_run))
    print(state["report_md"])
```

- [ ] **步骤 4：让 `run_once` 支持新参数**

把 `run_once` 的签名与 baseline 之后的部分改为：

```python
async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None,
                   only_test: str | None = None,
                   dry_run: bool = False) -> AifixState:
```

并在 `state.update(await baseline_node(state))` 之后插入：

```python
            if only_test is not None:
                state["queue"] = [t for t in state["queue"] if t == only_test]
            if dry_run:
                trace.fact("dry_run", True)
                state["queue"] = []
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/test_cli_args.py -q
```

预期：6 passed。

- [ ] **步骤 6：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/cli.py tests/test_cli_args.py
git commit -m "feat(cli): --test / --budget / --dry-run

--dry-run 只跑 preflight + baseline 报告有多少活，不调用任何模型 ——
接一个陌生项目时先看清工作量再决定要不要放它跑。"
```

---

## M2 完成标志

- [ ] `uv run pytest -q` 全绿
- [ ] 交付分支只含源码：`git diff --name-only main..aifix/<run_id>` 不含任何构建产物
- [ ] `.aifix/runs/<run_id>/` 下有 `report.md`、`facts.jsonl`、`events.jsonl`
- [ ] 真实模型跑一次，人为制造一个「模型不改任何文件」的场景，确认空 diff 守卫触发并重试
- [ ] 真实模型跑一次预算极小的场景（`--budget 0.01`），确认预算中止而非跑飞

最后两条需手动执行：

```bash
export AIFIX_FIXER__API_KEY=... AIFIX_FIXER__BASE_URL=https://api.deepseek.com \
       AIFIX_FIXER__MODEL=deepseek-v4-pro
export AIFIX_DETECTOR__API_KEY=$AIFIX_FIXER__API_KEY \
       AIFIX_DETECTOR__BASE_URL=$AIFIX_FIXER__BASE_URL \
       AIFIX_DETECTOR__MODEL=deepseek-v4-flash
cd /path/to/buggy/project && aifix run --budget 0.01
```

## 交给 M3 的缺口

| 缺口 | 说明 |
|---|---|
| `MavenAdapter` | 验证适配层抽象是否真的成立 |
| `aifix mine` | 从 git history 挖任务集 |
| `aifix eval` | 并行跑任务集 + 双档打分 |
| `aifix replay` | 消费 `events.jsonl` 做逐步重演 |
| 跨模型对比表 | 定位准确率 / 修复成功率 / 平均成本 / 越界尝试 |
