from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolError
from harness.tools.builtins._sandbox_util import format_exec

from ..adapters.base import ProjectAdapter


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
        cmd = self._adapter.scoped_test_command(params.test_ids)
        try:
            res = await self._sandbox.exec(cmd, self._timeout)
        finally:
            # 报告路径不再由调用方指定，只能问适配器要 —— 而且可能不止一份。
            # 不删干净的代价不是「被提交进交付分支」（Worktree.commit 只
            # add 记账过的路径，没有 git add -A 这条路），而是**陈旧报告被
            # 下一跑当成自己的结果**：这个工具与 run_scoped 写的是同一份
            # scoped 报告，而 report_paths 只看文件系统当前状态 —— 留在原地
            # 的话，verify 的 flaky 确认那一跑即便被超时杀掉、什么都没写出来，
            # 也会解析到这里剩下的这份并当成自己的结论。
            # 同一条理由见 nodes/baseline._rm_reports。
            stale = self._adapter.report_paths(
                Path(self._sandbox.workspace), scoped=True)
            if stale:
                await self._sandbox.exec(
                    ["rm", "-f", *(str(p) for p in stale)], 10.0)
        # 测试失败不是工具失败：结果原样回给模型判断，不抛 ToolError
        return format_exec(res, self._max_chars)
