from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolError

from .guard import guard_write

# 取 diff 头上的**原样**路径：`--- a/x.py` / `+++ b/x.py`。前缀在
# `_strip_p1` 里按 git 的规则剥，不在这里 —— 见那个函数的说明。
_TARGET = re.compile(r"^(?:---|\+\+\+)\s+(?P<path>\S+)", re.M)

_PATCH_FILE = ".aifix_patch.diff"

# `--recount`：不信 `@@ -49,5 +49,5 @@` 里那两个数，从正文推。
#
# 实跑打出来的：一次 run 里 15 次 apply_patch **12 次**栽在 `corrupt patch at
# line N`，326k tokens 修两个单行 bug（同样的活上一次只花 89k）。每一份 diff
# 的内容都是对的，错的只是那两个计数 —— 模型数不准自己写了几行，而 git 默认
# 严格按头里的数去读正文，读不满就判整个补丁损坏。这是模型手写 diff 的常见
# 失败形态，也正是 git 造这个开关的原因（「编辑过补丁但没调整 hunk 头」）。
#
# 不是放松校验：上下文仍然要逐行对得上，打错位置照样拒。放松的只是「模型得
# 会数数」这一条与修复正确性无关的要求。
#
# check 与真正应用必须用同一组参数，否则 check 是在验另一件事 —— 一个
# 「dry run 通过、真跑失败」的工具比没有 dry run 更坏。
_GIT_APPLY = ["git", "apply", "--recount"]

# 打不上有两种，建议必须分开。实跑里模型照着「你对文件当前内容的理解有误，
# 请先 read_file 确认」去做，重读了 16 次同一个文件、grep 了 14 次，然后交出
# 一份同样有结构问题的 diff —— 那句建议把它带进了死循环。
_ADVICE_CONTEXT = ("通常说明你对文件当前内容的理解有误，"
                   "请先 read_file 确认后重新生成 diff。")
_ADVICE_MALFORMED = ("这是 diff 的**格式**问题，不是文件内容问题 —— 重读文件没有用。"
                     "请检查：每一行正文必须以空格、`-` 或 `+` 开头（空行也要有那个"
                     "前导空格），`@@` 头与 `--- / +++` 头齐全。"
                     "`@@` 里的**行数不用你数**，工具会从正文重算。")


def _apply_error(stderr: str) -> str:
    """把 git 的报错转成模型能照着改的一句话。"""
    malformed = ("corrupt patch" in stderr
                 or "unrecognized input" in stderr
                 or "patch fragment without header" in stderr)
    advice = _ADVICE_MALFORMED if malformed else _ADVICE_CONTEXT
    return f"补丁无法应用（git apply --check 失败）：{stderr}\n{advice}"


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
        "对工作区文件应用 unified diff。**改代码请优先用 edit_file** —— "
        "这个工具要求你逐字复述上下文行，抄错一个字符补丁就打不上；"
        "只在需要一次改多个文件时用它。"
        "`@@` 里的行数不用你数，工具会从正文重算。"
        "只接 diff，不接整文件覆写；新建文件用 /dev/null 作为源。"
        "打不上时会返回 git 的具体报错，据此修正后重试。不允许修改测试文件。")

    class Params(BaseModel):
        diff: str = Field(description="标准 unified diff，须含 --- / +++ 文件头")

    def __init__(self, sandbox: Sandbox, is_test: Callable[[str], bool],
                 timeout: float = 60.0, touched: set[str] | None = None) -> None:
        self._sandbox = sandbox
        # 谓词由适配器给（`ProjectAdapter.is_test_path`）。原先收的是目录列表，
        # 而目录表达不了 vitest 的同目录布局 —— 见 guard.guard_write。
        self._is_test = is_test
        self._timeout = timeout
        # 本次 run 中被成功应用的补丁触及的路径。这份记账是交付时
        # `git add -- <paths>` 的**全部**输入（delivery.Worktree.commit），
        # 也是 nodes/fix._diff_lines 统计巨型 diff 的名单。
        # 交付路径上之所以不需要 git add -A（仓库里也确实没有），正是因为有
        # 它：agent 只能经由这个工具改文件，所以「改过哪些」是已知的，交付侧
        # 不必靠「把工作区里变了的东西全扫进来」去猜，测试产物、缓存这些未跟踪
        # 垃圾也就没有机会混进分支。
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
        """三道检查在 tools.guard 里，与 `edit_file` 共用同一份。

        判据是适配器给的谓词，不是目录列表：Maven 标准布局按分段前缀比
        （只看首段会把 `src/test/java/...` 当成源文件放行），vitest 按 `.test.ts`
        后缀比 —— 这里不该知道是哪一种。原样路径与写入路径两条都查，守卫宁可
        多拦不可漏放。理由与实现都在 guard.guard_write。
        """
        if not targets:
            raise ToolError("diff 里没有找到 --- / +++ 文件头，无法确定要改哪个文件。")
        for raw, real in targets:
            guard_write(self._sandbox, self._is_test, real, raw)

    async def run(self, params: "ApplyPatchTool.Params") -> str:
        targets = self._targets(params.diff)
        self._guard(targets)

        # 末尾补换行：`git apply` 对最后一行没有换行的补丁会直接判 corrupt，
        # 而模型漏掉结尾那个 `\n` 是常事 —— 这与 `--recount` 治的是同一类
        # 「与修复正确性无关的形式要求」。
        body = params.diff if params.diff.endswith("\n") else params.diff + "\n"
        await self._sandbox.write_file(_PATCH_FILE, body)
        try:
            check = await self._sandbox.exec(
                [*_GIT_APPLY, "--check", _PATCH_FILE], self._timeout)
            if check.exit_code != 0:
                raise ToolError(_apply_error(
                    check.stderr.strip() or check.stdout.strip()))
            applied = await self._sandbox.exec(
                [*_GIT_APPLY, _PATCH_FILE], self._timeout)
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
