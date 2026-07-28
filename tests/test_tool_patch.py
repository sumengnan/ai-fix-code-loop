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


_TOUCHES_MAVEN_TEST = """--- a/src/test/java/demo/CalcTest.java
+++ b/src/test/java/demo/CalcTest.java
@@ -1,2 +1,2 @@
 class CalcTest {
-  void t() { assertEquals(5, Calc.add(2, 3)); }
+  void t() { }
"""

_TOUCHES_MAVEN_SRC = """--- a/src/main/java/demo/Calc.java
+++ b/src/main/java/demo/Calc.java
@@ -1,2 +1,2 @@
 class Calc {
-  static int add(int a, int b) { return a - b; }
+  static int add(int a, int b) { return a + b; }
"""


async def _maven_guard(buggy_repo, diff: str):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = ToolRegistry()
        # Maven 标准布局：测试在 src/test/java 下，test_dirs 是 ["src/test"]
        reg.register(ApplyPatchTool(sb, test_dirs=["src/test"]))
        ex = ToolExecutor(reg, max_chars=8000)
        return await ex.execute(ToolCall(id="1", name="apply_patch",
                                         arguments={"diff": diff}))
    finally:
        await sb.close()


async def test_rejects_test_file_edit_in_a_nested_test_dir(buggy_repo):
    """M5 的 MavenAdapter：test_dirs = ["src/test"]，判据不能只看首段。

    只看 parts[0] 的话，`src/test/java/demo/CalcTest.java` 的首段是 `src`，
    守卫直接放行 —— 这个项目最核心的一道守卫（不许改测试）静默失效，
    模型可以靠删断言把任何用例改绿。
    """
    r = await _maven_guard(buggy_repo, _TOUCHES_MAVEN_TEST)
    assert r.is_error
    assert "测试" in r.content


async def test_nested_test_dir_guard_does_not_swallow_the_source_tree(
        buggy_repo):
    """区分度：同一棵树下的 src/main 必须照常放行。

    不加这一条的话，一个「凡路径含 src 就拒」的实现也能让上面那条变绿，
    而它会把所有源码修改一并挡死 —— 守卫从失效变成全拒，同样是坏的。
    """
    r = await _maven_guard(buggy_repo, _TOUCHES_MAVEN_SRC)
    # 这个 diff 打不上（buggy_repo 里没有这个文件），但必须是 git 的报错，
    # 不能是守卫的「拒绝修改测试文件」
    assert "拒绝修改测试文件" not in r.content


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
