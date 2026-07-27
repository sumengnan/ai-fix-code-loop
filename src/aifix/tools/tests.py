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
