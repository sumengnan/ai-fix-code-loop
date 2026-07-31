from __future__ import annotations

import re

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolError

from .guard import guard_write

# 取 diff 头上的**原样**路径：`--- a/x.py` / `+++ b/x.py`。前缀在
# `_strip_p1` 里按 git 的规则剥，不在这里 —— 见那个函数的说明。
_TARGET = re.compile(r"^(?:---|\+\+\+)\s+(?P<path>\S+)", re.M)

_PATCH_FILE = ".aifix_patch.diff"


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def repair_diff(diff: str) -> str:
    """修掉模型生成 unified diff 时的两种**机械**错误。起止行号与正文内容不动。

    一、`@@ -a,b +c,d @@` 的行数按正文重算。
    二、hunk 内部的**裸空行**补上前导空格 —— 空的上下文行必须是 `" "`，
        而模型（以及沿途任何 strip 尾空格的环节）经常发成真空行。

    **实测逼出来的**（2026-07-31，qwen3-coder-flash 的 39 任务评测）：
    `apply_patch` 被调 332 次、**失败 309 次**，几乎全是 `corrupt patch at
    line N` —— 而 corrupt 不是「上下文对不上」，是**这个 diff 语法就坏的**：
    表头的行数与正文对不上。样本二是 `@@ -39,12 +39,14 @@` 而正文有 3 个新增，
    新侧应为 15。

    数行数正是 LLM 最不擅长的事，而**正文往往是对的**。模型于是陷在
    「read_file 431 次 / apply_patch 332 次」的循环里烧完 50 万 token，
    一个字节都没改。

    **为什么重算是安全的、而不是在掩盖错误**：表头修好之后，`git apply
    --check` 该拒还是拒 —— 只是理由换成了真实的那一个（上下文不匹配），
    而那个理由对应的建议（重新 read_file）才是有用的。这个函数消灭的是
    一种**机械故障**，不是一种判定。

    只改表头，**一个正文字节都不动**：顺手改正文等于替模型改代码，那是
    apply_patch 的越权。认不出来的原样返回 —— 看不懂时闭嘴让 git 去报错，
    它的报错至少是准的。
    """
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _HUNK.match(lines[i].rstrip("\n"))
        if m is None:
            out.append(lines[i]); i += 1
            continue
        # 数正文：上下文行两侧都算，`\ No newline` 之类一律跳过
        old_n = new_n = 0
        j = i + 1
        while j < len(lines):
            ch = lines[j][:1]
            if ch == "@" and lines[j].startswith("@@"):
                break
            if lines[j].startswith(("--- ", "+++ ", "diff --git ")):
                break
            if ch == "\\":
                j += 1; continue
            # `"\n"` 是**裸空行**（该有的前导空格丢了），`""` 是文件末尾没有
            # 换行的那一行。两者都是空的上下文行 —— 漏掉任一个，计数会在这里
            # 提前 break，表头被算成 `@@ -1,1 +1,1 @@`。这个坑就是要修的那个
            # 字符自己造成的。
            if ch in (" ", "", "\n"):
                old_n += 1; new_n += 1
            elif ch == "-":
                old_n += 1
            elif ch == "+":
                new_n += 1
            else:
                break
            j += 1
        a, c, tail = m.group(1), m.group(3), m.group(5)
        out.append(f"@@ -{a},{old_n} +{c},{new_n} @@{tail}\n")
        # 裸空行补回前导空格。**只补空行**，别的一律原样 —— 这一步是把
        # 「格式上不合法」变成「格式合法」，不是替模型改内容。
        for ln in lines[i + 1:j]:
            out.append(" \n" if ln == "\n" else ln)
        i = j
    return "".join(out)


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
        "这个工具要求你数对 `@@` 里的行数，出错的补丁一个字节也改不动；"
        "只在需要一次改多个文件时用它。"
        "只接 diff，不接整文件覆写；新建文件用 /dev/null 作为源。"
        "打不上时会返回 git 的具体报错，据此修正后重试。不允许修改测试文件。")

    class Params(BaseModel):
        diff: str = Field(description="标准 unified diff，须含 --- / +++ 文件头")

    def __init__(self, sandbox: Sandbox, test_dirs: list[str],
                 timeout: float = 60.0, touched: set[str] | None = None) -> None:
        self._sandbox = sandbox
        self._test_dirs = [d.strip("/") for d in test_dirs]
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

        分段前缀、不是首段：Maven 标准布局的 test_dirs 是 `["src/test"]`，
        只看首段会把 `src/test/java/...` 当成源文件放行。原样路径与写入路径
        两条都查 —— 守卫宁可多拦不可漏放。理由与实现都在 guard.guard_write。
        """
        if not targets:
            raise ToolError("diff 里没有找到 --- / +++ 文件头，无法确定要改哪个文件。")
        for raw, real in targets:
            guard_write(self._sandbox, self._test_dirs, real, raw)

    async def run(self, params: "ApplyPatchTool.Params") -> str:
        targets = self._targets(params.diff)
        self._guard(targets)

        # 先修机械错误再交给 git：模型算不对 hunk 行数是 100% 的坏补丁成因
        # （见 repair_diff 里那段实测）。修完之后 git 该拒还是拒，只是理由
        # 换成了真实的那一个。
        raw = params.diff if params.diff.endswith("\n") else params.diff + "\n"
        body = repair_diff(raw)
        await self._sandbox.write_file(_PATCH_FILE, body)
        try:
            check = await self._sandbox.exec(
                ["git", "apply", "--check", _PATCH_FILE], self._timeout)
            if check.exit_code != 0:
                err = check.stderr.strip() or check.stdout.strip()
                # **两种失败要给不同的建议**，混成一句会把模型指向走不通的路。
                # 实测（2026-07-31）：309 次失败几乎全是 corrupt patch（语法坏），
                # 而当时统一给的建议是「请先 read_file」—— 那是给「上下文对不上」
                # 的话。于是模型 read_file 431 次、apply_patch 332 次，卡死在
                # 一个再读一百遍也解决不了的循环里，烧完 50 万 token 一字未改。
                if "corrupt patch" in err:
                    raise ToolError(
                        f"补丁的**格式**不合法（git 拒绝解析）：{err}\n"
                        "这不是「文件内容对不上」——**再 read_file 也没用**。\n"
                        "检查 diff 本身：每个上下文行要以一个空格开头、"
                        "删除行以 `-`、新增行以 `+`；空行也必须是一个空格。\n"
                        "hunk 头的行数由工具自动重算，你不必去数。")
                raise ToolError(
                    f"补丁无法应用（git apply --check 失败）：{err}\n"
                    "格式是合法的，是**上下文对不上** —— 你对文件当前内容的"
                    "理解有误。先 read_file 确认那几行的原样，再重新生成 diff。")
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
