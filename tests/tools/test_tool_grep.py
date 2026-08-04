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
