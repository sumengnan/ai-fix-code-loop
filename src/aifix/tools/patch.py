from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, SandboxError, resolve_in_workspace
from harness.tools.base import Tool, ToolError

from ..signals import under_dirs

# 取 diff 头上的**原样**路径：`--- a/x.py` / `+++ b/x.py`。前缀在
# `_strip_p1` 里按 git 的规则剥，不在这里 —— 见那个函数的说明。
_TARGET = re.compile(r"^(?:---|\+\+\+)\s+(?P<path>\S+)", re.M)

_PATCH_FILE = ".aifix_patch.diff"


def _strip_p1(path: str) -> str:
    """按 `git apply` 默认的 `-p1` 剥前缀：有 `/` 就剥掉第一段，没有就原样。

    守卫必须和 git 看同一条路径，否则它拦的是一个不存在的东西。曾经这里只
    认 `a/` 与 `b/` 两种前缀，而 `-p1` 剥掉的是**任意**第一段：喂进
    `--- x/tests/test_add.py`，守卫读到的首段是 `x`（不在 test_dirs 里，放
    行），git 剥掉 `x/` 之后写的却是 `tests/test_add.py` —— 断言被删掉，工具
    还回「补丁已应用」。这个项目最核心的一道守卫可以被路径写法整个绕过。

    「没有 `/` 就原样保留」不是补丁而是 git 的真实行为（apply.c 的
    `stripath`：分段不够就停下）：`diff.noprefix` 风格的 `--- calc.py` 会被
    写进 `calc.py`，无条件丢掉第一段会得到空路径，守卫和路径围栏都拿它没辙。
    """
    _first, sep, rest = path.partition("/")
    return rest if sep and rest else path


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

    def _targets(self, diff: str) -> list[tuple[str, str]]:
        """(diff 头上的原样路径, git 实际写入的路径) 去重后的列表。

        两个都留着：写入路径是 git 的真相（记账与路径围栏按它），原样路径则
        兜住 `diff.noprefix` 风格的 diff（`--- tests/x.py`）—— 那种写法剥掉
        一段之后就不像测试文件了，但它本来就是。
        """
        seen: list[tuple[str, str]] = []
        for m in _TARGET.finditer(diff):
            raw = m.group("path")
            # /dev/null 是「这一侧不存在」的约定值，不是路径，不能剥前缀
            if raw == "/dev/null":
                continue
            pair = (raw, _strip_p1(raw))
            if pair not in seen:
                seen.append(pair)
        return seen

    def _guard(self, targets: list[tuple[str, str]]) -> None:
        if not targets:
            raise ToolError("diff 里没有找到 --- / +++ 文件头，无法确定要改哪个文件。")
        for raw, real in targets:
            if ".git" in PurePosixPath(real).parts:
                raise ToolError(f"拒绝修改 .git 目录下的文件：{real}")
            # 分段前缀，不是首段：Maven 标准布局的 test_dirs 是
            # `["src/test"]`，只看首段会把 `src/test/java/...` 当成源文件放
            # 行。判定与 eval/mine.split_paths 共用 signals.under_dirs。
            # 原样路径与写入路径两条都查：守卫宁可多拦不可漏放，而这两条里
            # 任何一条像测试文件，这个 diff 就不值得放行。
            if under_dirs(real, self._test_dirs) or under_dirs(
                    raw, self._test_dirs):
                raise ToolError(
                    f"拒绝修改测试文件：{real}。"
                    "请修改源码使测试通过，而不是修改测试本身。")
            # 路径围栏：逃逸工作区抛 SandboxError。按写入路径判 —— 围栏问的是
            # 「git 会往哪写」，原样路径带着前缀，判出来的是另一个位置。
            resolve_in_workspace(self._sandbox.workspace, real)

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
            # 只有真正写进去了才记账，且记的是 git 写入的路径 —— 记成
            # `a/calc.py` 的话，交付时 `git add -- a/calc.py` 匹配不到任何
            # 文件，交付分支上一个提交都没有，报告却照写「已修复」。
            if self._touched is not None:
                self._touched.update(real for _, real in targets)
            stat = await self._sandbox.exec(
                ["git", "diff", "--stat"], self._timeout)
            return "补丁已应用。当前改动：\n" + (stat.stdout.strip() or "（无）")
        finally:
            # 临时文件必须清掉：它是未跟踪文件，留着会干扰后续判断
            await self._sandbox.exec(["rm", "-f", _PATCH_FILE], 10.0)
