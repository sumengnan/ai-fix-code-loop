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
