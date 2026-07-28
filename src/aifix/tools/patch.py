from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, SandboxError, resolve_in_workspace
from harness.tools.base import Tool, ToolError

from ..signals import under_dirs

# 取 diff 里的目标路径：`--- a/x.py` / `+++ b/x.py`，忽略 /dev/null
_TARGET = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(?P<path>\S+)", re.M)

_PATCH_FILE = ".aifix_patch.diff"


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "对工作区文件应用 unified diff。只接 diff，不接整文件覆写；"
        "新建文件用 /dev/null 作为源。打不上时会返回 git 的具体报错，"
        "据此修正后重试。不允许修改测试文件。")

    class Params(BaseModel):
        diff: str = Field(description="标准 unified diff，须含 --- / +++ 文件头")

    def __init__(self, sandbox: Sandbox, test_dirs: list[str],
                 timeout: float = 60.0, touched: set[str] | None = None) -> None:
        self._sandbox = sandbox
        self._test_dirs = [d.strip("/") for d in test_dirs]
        self._timeout = timeout
        # 本次 run 中被成功应用的补丁触及的路径。交付时只提交这些文件，
        # 避免 git add -A 把测试产物、缓存等未跟踪垃圾扫进分支。
        self._touched = touched

    def _targets(self, diff: str) -> list[str]:
        seen: list[str] = []
        for m in _TARGET.finditer(diff):
            p = m.group("path")
            if p == "/dev/null" or p in seen:
                continue
            seen.append(p)
        return seen

    def _guard(self, targets: list[str]) -> None:
        if not targets:
            raise ToolError("diff 里没有找到 --- / +++ 文件头，无法确定要改哪个文件。")
        for p in targets:
            parts = PurePosixPath(p).parts
            if ".git" in parts:
                raise ToolError(f"拒绝修改 .git 目录下的文件：{p}")
            # 分段前缀，不是首段：Maven 标准布局的 test_dirs 是
            # `["src/test"]`，只看首段会把 `src/test/java/...` 当成源文件放
            # 行。判定与 eval/mine.split_paths 共用 signals.under_dirs。
            if under_dirs(p, self._test_dirs):
                raise ToolError(
                    f"拒绝修改测试文件：{p}。"
                    "请修改源码使测试通过，而不是修改测试本身。")
            # 路径围栏：逃逸工作区抛 SandboxError
            resolve_in_workspace(self._sandbox.workspace, p)

    async def run(self, params: "ApplyPatchTool.Params") -> str:
        targets = self._targets(params.diff)
        try:
            self._guard(targets)
        except SandboxError as e:
            raise ToolError(str(e))

        body = params.diff if params.diff.endswith("\n") else params.diff + "\n"
        await self._sandbox.write_file(_PATCH_FILE, body)
        try:
            check = await self._sandbox.exec(
                ["git", "apply", "--check", _PATCH_FILE], self._timeout)
            if check.exit_code != 0:
                raise ToolError(
                    "补丁无法应用（git apply --check 失败）："
                    f"{check.stderr.strip() or check.stdout.strip()}\n"
                    "通常说明你对文件当前内容的理解有误，"
                    "请先 read_file 确认后重新生成 diff。")
            applied = await self._sandbox.exec(
                ["git", "apply", _PATCH_FILE], self._timeout)
            if applied.exit_code != 0:
                raise ToolError(
                    f"补丁应用失败：{applied.stderr.strip() or applied.stdout.strip()}")
            # 只有真正写进去了才记账
            if self._touched is not None:
                self._touched.update(targets)
            stat = await self._sandbox.exec(
                ["git", "diff", "--stat"], self._timeout)
            return "补丁已应用。当前改动：\n" + (stat.stdout.strip() or "（无）")
        finally:
            # 临时文件必须清掉：它是未跟踪文件，留着会干扰后续判断
            await self._sandbox.exec(["rm", "-f", _PATCH_FILE], 10.0)
