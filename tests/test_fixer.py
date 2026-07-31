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


async def test_registry_exposes_exactly_the_whitelisted_tools(buggy_repo):
    """能力面是**白名单**：这里写死一个集合，多一个少一个都要红。

    2026-07-31 加了两个：`edit_file`（首选的修改方式，不用数 diff 行号）与
    `read_symbol`（按名字读完整定义）。加工具是扩大攻击面，所以这条断言
    必须是等号而不是包含 —— 将来谁顺手注册一个工具，得先在这里说明白。
    """
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids={_FAILURE.test_id})
        assert {t.name for t in reg.tools()} == {
            "read_file", "read_symbol", "list_files", "grep",
            "edit_file", "apply_patch", "run_tests"}
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
        reg = build_registry(sb, PytestAdapter(), known_ids=set(),
                             touched=touched)
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
