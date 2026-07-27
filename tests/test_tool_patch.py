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
