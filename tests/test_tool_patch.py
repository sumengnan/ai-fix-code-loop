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


# ---------------------------------------------------------- 伪造前缀绕过守卫
#
# 这三个 diff 的正文逐字相同（把 test_add 的断言换成 assert True），只有
# `--- / +++` 那两行的路径写法不同。守卫看到的路径必须与 git 真正写入的路径
# 是同一个，否则「不许改测试文件」这道守卫可以被路径写法整个绕过。

def _kill_assert(header_path: str) -> str:
    return (f"--- {header_path}\n"
            f"+++ {header_path}\n"
            "@@ -4,3 +4,3 @@\n"
            " def test_add():\n"
            "-    assert add(2, 3) == 5\n"
            "+    assert True\n"
            " \n")


_FORGED_PREFIX = _kill_assert("x/tests/test_calc.py")
_UPPERCASE_DIR = _kill_assert("a/TESTS/test_calc.py")


async def test_forged_path_prefix_cannot_bypass_the_test_guard(executor):
    """`git apply` 默认 -p1，剥掉的是**任意**第一段，不只是 `a/` / `b/`。

    只认 `a/` / `b/` 的话，`x/tests/test_calc.py` 在守卫眼里首段是 `x`
    （不在 test_dirs 里，放行），而 git 剥掉 `x/` 后写的是
    `tests/test_calc.py` —— 断言真的被删掉，工具还回「补丁已应用」。
    """
    ex, repo = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _FORGED_PREFIX}))
    body = (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")
    # 磁盘先看：即使工具报了错，只要文件被改了，这道守卫就是失效的
    assert "assert add(2, 3) == 5" in body, "测试文件被改写了"
    assert "assert True" not in body
    assert r.is_error
    assert "测试" in r.content


async def test_uppercase_test_dir_cannot_bypass_the_test_guard(executor):
    """守卫大小写敏感、而 macOS / Windows 的文件系统不敏感。

    `a/TESTS/test_calc.py` 在守卫眼里不是 `tests` 目录，git 却把它写进了
    `tests/test_calc.py`。守卫宁可多拦不可漏放，判定必须大小写不敏感。
    """
    ex, repo = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _UPPERCASE_DIR}))
    body = (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")
    assert "assert add(2, 3) == 5" in body, "测试文件被改写了"
    assert "assert True" not in body
    assert r.is_error
    assert "测试" in r.content


_NO_PREFIX_SOURCE = """--- calc.py
+++ calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""


async def test_single_segment_path_is_not_stripped(executor):
    """区分度：`git apply -p1` 只在有 `/` 时才剥一段。

    不加这一条的话，一个「无条件丢掉第一段」的实现也能让上面两条变绿，
    而它会把 `--- calc.py`（diff.noprefix 风格）剥成空路径，守卫拿着空路径
    去判、去做围栏检查，行为完全不可预测。git 对它的处理是原样保留。
    """
    ex, repo = executor
    r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                  arguments={"diff": _NO_PREFIX_SOURCE}))
    assert not r.is_error, r.content
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")


async def test_touched_records_the_path_git_actually_writes(buggy_repo):
    """记账记的必须是 git 写入的路径，不是 diff 头上的原样字符串。

    记成 `a/calc.py` 的话，交付时 `git add -- a/calc.py` 匹配不到任何文件，
    交付分支上一个提交都没有 —— 报告却照写「已修复」。
    """
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        touched: set[str] = set()
        reg = ToolRegistry()
        reg.register(ApplyPatchTool(sb, test_dirs=["tests"], touched=touched))
        ex = ToolExecutor(reg, max_chars=8000)
        r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                      arguments={"diff": _NO_PREFIX_SOURCE}))
        assert not r.is_error, r.content
        assert touched == {"calc.py"}
    finally:
        await sb.close()
