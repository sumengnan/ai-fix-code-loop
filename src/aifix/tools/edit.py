"""按**原文**改代码：给出旧的一段、新的一段，定位交给确定性代码。

为什么在已经有 `apply_patch` 之后还要加这个（2026-07-31，qwen3-coder-flash
的 39 任务评测）：

    apply_patch 调用 332 次，失败 309 次（93%）
    坏补丁成因：247/247 是 `@@ -a,b +c,d @@` 的行数与正文对不上（100%）

`repair_diff` 把这一类语法故障消掉了 97%，但它治的是症状。真正的问题在接口
本身：unified diff 要求模型**逐字复述上下文行**、**数对两侧行数**、**给对起始
行号**，把「我要把这段改成那段」编码成了一道算术题 —— 而算术正是 LLM 结构性
最弱的能力。模型的正文往往是对的，它栽在记账上。

edit_file 把记账拿走。没有行号、没有计数、没有上下文行前缀，能算错的东西
不存在。代价是必须**原样复述**要改的那一段 —— 那是复制，不是计算。

`apply_patch` 保留：跨越大段的重排、一次动多个文件，diff 仍然是更好的表达。
两条路共用 `guard.guard_write`，围栏上不会因为多一条路而多一个洞。

三处刻意的设计，都来自「报错要指向能走通的路」这条教训：

- **找不到原文时，把文件里最像的那几行连行号一起还回去。** 「没找到」是死路，
  模型只能再猜一遍；把真实内容给它，下一步就能改对。
- **只差缩进的，明说是缩进，但不替它猜。** 猜错就是静默写进去一段坏代码。
- **出现多次一律拒绝**，并报出每一处的行号 —— 随便挑一处改，改错了没人知道。
"""
from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, resolve_in_workspace
from harness.tools.base import Tool, ToolError

from .guard import guard_write

# 报错里最多列几处/几条候选。给三条足够定位，给一屏会把真正的线索淹掉。
_MAX_HINTS = 3


def _line_of(text: str, index: int) -> int:
    """字符下标 → 行号（1 起）。"""
    return text.count("\n", 0, index) + 1


def _full_lines(s: str) -> list[str] | None:
    """`s` 作为**整行块**时的行列表；不是整行块（末尾没有换行）则 None。

    宽松匹配只对整行块开放：那是行尾空白真正会出问题的场合（多行复制），
    而片段替换一旦放宽边界，替换的起止就不再由原文唯一决定了。
    """
    return s[:-1].split("\n") if s.endswith("\n") else None


def _loose_spans(lines: list[str], block: list[str]) -> list[int]:
    """按「行尾空白不算差异」找整行块，返回起始行下标（0 起）。

    行尾空白在传输、复制、渲染的任一环都可能被吃掉，而它对代码语义**没有
    任何影响**。这一类不值得占用模型一个回合。
    """
    want = [ln.rstrip() for ln in block]
    n = len(want)
    if n == 0:
        return []
    got = [ln.rstrip() for ln in lines]
    return [i for i in range(len(got) - n + 1) if got[i:i + n] == want]


def _indent_only_spans(lines: list[str], old: str) -> list[int]:
    """只差缩进的位置。**仅用于报错**，绝不据此改文件。

    替模型猜缩进就是替它写代码：猜错写进去的是一段坏代码，而且没有任何
    输出会提到这件事。把原样的行给它、让它重写，是唯一诚实的做法。
    """
    want = [ln.strip() for ln in old.split("\n")]
    while want and want[-1] == "":
        want.pop()
    n = len(want)
    if n == 0:
        return []
    got = [ln.strip() for ln in lines]
    return [i for i in range(len(got) - n + 1) if got[i:i + n] == want]


def _closest(lines: list[str], probe: str) -> list[tuple[int, str]]:
    """文件里最像 `probe` 的几行，带行号。相似度低于阈值的一条都不给 ——
    给一堆不相干的行等于制造噪声，而模型会认真对待它们。"""
    key = probe.strip()
    if not key:
        return []
    scored = [(difflib.SequenceMatcher(None, key, ln.strip()).ratio(), i, ln)
              for i, ln in enumerate(lines)]
    scored.sort(key=lambda t: -t[0])
    return [(i + 1, ln) for r, i, ln in scored[:_MAX_HINTS] if r >= 0.5]


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "把文件里一段**原样的**文本替换成新文本 —— 改代码优先用这个，"
        "不用写 diff、不用数行号。old_text 必须与文件里现有内容逐字相同"
        "（可从 read_file 的输出里去掉行号后照抄），且在文件中唯一；"
        "多处相同时往 old_text 里多带几行上下文，或显式 replace_all。"
        "old_text 留空表示新建文件。不允许修改测试文件。")

    class Params(BaseModel):
        path: str = Field(description="相对工作区根目录的路径")
        old_text: str = Field(
            description="文件中现有的一段原文，逐字相同；留空表示新建文件")
        new_text: str = Field(description="用来替换它的新文本")
        replace_all: bool = Field(
            default=False, description="原文出现多次时是否全部替换")

    def __init__(self, sandbox: Sandbox, test_dirs: list[str],
                 touched: set[str] | None = None) -> None:
        self._sandbox = sandbox
        self._test_dirs = test_dirs
        # 与 apply_patch 同一份记账：交付时 `git add -- <paths>` 的全部输入。
        # 漏记 = 改动不进交付分支，而报告照写「已修复」。
        self._touched = touched

    async def run(self, params: "EditFileTool.Params") -> str:
        guard_write(self._sandbox, self._test_dirs, params.path)
        # resolve_in_workspace 返回**字符串**，不是 Path —— 踩过一次
        p = Path(resolve_in_workspace(self._sandbox.workspace, params.path))

        if params.old_text == "":
            return self._create(p, params)

        if params.old_text == params.new_text:
            raise ToolError(
                "old_text 与 new_text 完全相同，这次调用不会改变任何东西。"
                "如果你以为自己做了修改，请重新确认要改的是哪一段。")

        try:
            # `newline=""` 关掉**通用换行翻译**。不关的话 Python 读进来时把
            # `\r\n` 一律变成 `\n`，写回去就成了 LF —— 整个文件的行尾被换掉，
            # 而模型只想改一行。这不是「被替换的那几行混了行尾」，是**全文件
            # 级别的静默改写**：git 会把它显示成每一行都改了。
            with p.open(encoding="utf-8", newline="") as f:
                text = f.read()
        except FileNotFoundError:
            raise ToolError(
                f"文件不存在：{params.path}。"
                "新建文件请把 old_text 留空。") from None
        except IsADirectoryError:
            raise ToolError(f"这是个目录，不是文件：{params.path}") from None
        except UnicodeDecodeError:
            # **不能**像 read_file 那样 errors="replace"：那边只是显示，这边要
            # 写回去 —— 把无法解码的字节替换成 U+FFFD 再整份写回，等于悄悄
            # 损坏一个二进制文件。读不动就不改。
            raise ToolError(
                f"{params.path} 不是 UTF-8 文本文件，拒绝修改。") from None

        hits = text.count(params.old_text)
        if hits > 1 and not params.replace_all:
            raise ToolError(self._ambiguous(text, params.old_text))
        if hits >= 1:
            if params.replace_all:
                out = text.replace(params.old_text, params.new_text)
                where = f"{hits} 处"
            else:
                out = text.replace(params.old_text, params.new_text, 1)
                where = f"第 {_line_of(text, text.index(params.old_text))} 行"
            return self._write(p, params.path, out, where)

        # 逐字对不上 —— 先试行尾空白，再判断是不是只差缩进，最后给出真实内容
        lines = text.split("\n")
        block = _full_lines(params.old_text)
        if block is not None:
            spans = _loose_spans(lines, block)
            if len(spans) > 1 and not params.replace_all:
                raise ToolError(self._ambiguous_msg(
                    [s + 1 for s in spans[:_MAX_HINTS + 1]]))
            if len(spans) == 1 or (spans and params.replace_all):
                new_lines = _full_lines(params.new_text)
                if new_lines is None:
                    new_lines = params.new_text.split("\n")
                # CRLF 文件：`text.split("\n")` 留下的每行尾部还挂着 `\r`，而
                # 模型写的 new_text 不会有。直接塞进去会让这几行变成 LF，
                # 整个文件混行尾 —— 而这一步**没有任何输出会提到它**。
                # 逐字命中那条路走 str.replace，不经过拆行，所以只有这里需要。
                if any(lines[s + k].endswith("\r")
                       for s in spans for k in range(len(block))):
                    new_lines = [ln if ln.endswith("\r") else ln + "\r"
                                 for ln in new_lines]
                for start in reversed(spans):
                    lines[start:start + len(block)] = new_lines
                where = (f"{len(spans)} 处" if len(spans) > 1
                         else f"第 {spans[0] + 1} 行")
                return self._write(p, params.path, "\n".join(lines), where)

        raise ToolError(self._not_found(lines, params.old_text, params.path))

    # ── 写入 ────────────────────────────────────────────────────────────

    def _create(self, p: Path, params: "EditFileTool.Params") -> str:
        if p.exists():
            raise ToolError(
                f"{params.path} 已存在。old_text 留空只用于新建文件 —— "
                "要改已有内容，请把要替换的那一段原文填进 old_text。")
        p.parent.mkdir(parents=True, exist_ok=True)
        self._put(p, params.new_text)
        if self._touched is not None:
            self._touched.add(params.path)
        n = len(params.new_text.splitlines())
        return f"已新建 {params.path}（{n} 行）。"

    @staticmethod
    def _put(p: Path, body: str) -> None:
        """`newline=""` 关掉写入侧的换行翻译 —— 与读入侧成对。

        少了任何一半，CRLF 文件都会被整份改成 LF：git 显示为「每一行都改了」，
        而模型只动了一行，回执里也只提了一行。
        """
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write(body)

    def _write(self, p: Path, rel: str, out: str, where: str) -> str:
        self._put(p, out)
        if self._touched is not None:
            self._touched.add(rel)
        return f"已修改 {rel}（{where}）。"

    # ── 报错 ────────────────────────────────────────────────────────────

    def _ambiguous(self, text: str, old: str) -> str:
        at: list[int] = []
        i = text.find(old)
        while i != -1 and len(at) < _MAX_HINTS + 1:
            at.append(_line_of(text, i))
            i = text.find(old, i + 1)
        return self._ambiguous_msg(at)

    @staticmethod
    def _ambiguous_msg(at: list[int]) -> str:
        head = "、".join(f"第 {n} 行" for n in at[:_MAX_HINTS])
        more = "" if len(at) <= _MAX_HINTS else " 等"
        return (
            f"old_text 在文件中出现了多次（{head}{more}），无法确定改哪一处。\n"
            "往 old_text 里多带几行上下文，让它唯一；"
            "确实要全部替换就传 replace_all=true。")

    def _not_found(self, lines: list[str], old: str, path: str) -> str:
        near = _indent_only_spans(lines, old)
        if near:
            n = near[0]
            body = "\n".join(f"{n + k + 1:>6}\t{lines[n + k]}"
                             for k in range(len(old.rstrip("\n").split("\n")))
                             if n + k < len(lines))
            return (
                f"old_text 没有逐字命中，但第 {n + 1} 行起有一处**只差缩进**。\n"
                f"文件里的原样如下（行号仅供参考，别抄进 old_text）：\n{body}\n"
                "请按上面的缩进重写 old_text。我不替你猜缩进 —— 猜错会静默地"
                "写进一段坏代码。")

        probe = next((ln for ln in old.split("\n") if ln.strip()), "")
        close = _closest(lines, probe)
        if close:
            body = "\n".join(f"{n:>6}\t{ln}" for n, ln in close)
            return (
                f"{path} 里没有找到 old_text 的原文。\n"
                f"最接近的几行是（这是文件当前的真实内容）：\n{body}\n"
                "按上面的原样重写 old_text。注意逐字相同，包括缩进与标点。")
        return (
            f"{path} 里没有找到 old_text 的原文，也没有相近的行 —— "
            "很可能是文件选错了。先用 grep 或 read_symbol 确认这段代码在哪个"
            "文件里。")
