import subprocess

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
    tid = "tests/test_calc.py::test_identity"
    a = PytestAdapter()
    await ex.execute(ToolCall(id="1", name="run_tests",
                              arguments={"test_ids": [tid]}))
    assert a.report_paths(repo, scoped=True) == []
    # 反证：同一条命令直接跑确实会留下报告。少了这一步，上面那句在
    # 「报告名改了、根本没人写这个文件」时也照样绿 —— 恒真断言。
    subprocess.run(a.scoped_test_command([tid]), cwd=repo,
                   capture_output=True, text=True)
    assert a.report_paths(repo, scoped=True), "复跑没写出报告，上一句断言不作数"
