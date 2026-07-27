from __future__ import annotations

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, resolve_in_workspace
from harness.tools.base import Tool, ToolError
from harness.tools.builtins._sandbox_util import truncate


class GrepTool(Tool):
    name = "grep"
    description = ("在工作区内按正则搜索代码，返回 `文件:行号:内容`。"
                   "底层是 git grep：自动跳过 .gitignore 里的路径。")

    class Params(BaseModel):
        pattern: str = Field(description="正则表达式")
        path: str = Field(default=".", description="限定搜索的子路径")
        max_results: int = Field(default=50, ge=1, le=200)

    def __init__(self, sandbox: Sandbox, timeout: float = 30.0,
                 max_chars: int = 8000) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._max_chars = max_chars

    async def run(self, params: "GrepTool.Params") -> str:
        # 路径围栏：逃逸抛 SandboxError，由 ToolExecutor 兜成 is_error
        resolve_in_workspace(self._sandbox.workspace, params.path)
        res = await self._sandbox.exec(
            ["git", "grep", "-n", "-I", "-E", params.pattern, "--", params.path],
            self._timeout)
        # git grep：0=有匹配，1=无匹配，其余为真错误
        if res.exit_code == 1 and not res.stderr.strip():
            return "无匹配。"
        if res.exit_code not in (0, 1):
            raise ToolError(f"搜索失败：{res.stderr.strip() or res.stdout.strip()}")
        lines = res.stdout.splitlines()[: params.max_results]
        more = "" if len(res.stdout.splitlines()) <= params.max_results else \
            f"\n…（已截断到前 {params.max_results} 条）"
        return truncate("\n".join(lines) + more, self._max_chars)
