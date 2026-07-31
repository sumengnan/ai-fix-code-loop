"""按名字读一个函数/类的**完整**定义 —— 边界由代码结构算出来，不靠猜。

为什么在 `read_file` 已经有 offset 之后还要加这个（2026-07-31 那次评测）：

    read_file 431 次 / apply_patch 332 次（失败 309）

模型知道自己要改哪个函数，却要 `grep 拿行号 → read_file 猜一个窗口 → 发现
切在一半 → 再 read_file`。offset 让它**能**读到尾巴，但每次仍然是在猜窗口：
猜小了截断、猜大了灌进几百行无关代码 —— 而上下文预算就是这么烧掉的。

函数的边界是**确定的**：Python 有 ast，花括号语言数括号。这件事没有任何理由
交给模型去猜。一次调用给出准确的起止，省下的是回合数，也是 token。

三处刻意的设计：

- **装饰器算在定义里。** `node.lineno` 指向 `def` 那一行，装饰器在它上面；
  直接用 lineno 会静默切掉 `@lru_cache` —— 而缓存正是这类缺陷的常见成因。
- **语法错误时退到正则，不抛。** 修复过程中源码处于半坏状态是常态（模型刚
  改了一半），这时候崩掉等于在最需要工具的时刻把工具拿走。
- **重名全给出来。** 随便挑一个返回是最坏的做法：模型会照着改，而它改的
  可能是另一个。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, SandboxError, resolve_in_workspace
from harness.tools.base import Tool, ToolError

_PY = {".py", ".pyi"}

# 文档与数据文件里**不找符号**。用黑名单而不是白名单：白名单会把没列到的
# 语言整个挡在外面，而这个工具的价值恰恰在于「不知道在哪也能找」。
#
# 实测（写完当天的冒烟）：查 `EditFileTool.run`，第一个命中的是
# `docs/superpowers/plans/2026-07-27-m1-minimal-loop.md` —— 计划文档里的代码块。
# 那份 markdown 里有 `def run(` 而缩进兜底认它，于是真正的实现被挤出了名额。
_SKIP_SUFFIX = {".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml",
                ".lock", ".csv", ".jsonl", ".log", ".xml", ".html", ".cfg",
                ".ini"}

# 一行「不可能是定义」的开头。花括号语言里 `if (check(x)) {` 长得和方法声明
# 很像，靠这个把控制流挡掉 —— 不挡的话，随便一个调用点都会被当成定义读出来。
_NOT_DECL = re.compile(
    r"^\s*(if|while|for|switch|return|else|catch|do|throw|case)\b")


def _py_spans(src: str, name: str) -> list[tuple[str, int, int]]:
    """ast 精确解析。返回 (限定名, 起始行, 结束行)，行号 1 起。

    `name` 可以是裸名（`total`）也可以是点分名（`Cart.total`）—— 后者按
    **后缀**匹配限定名。同名方法在一个仓库里很常见，只按裸名找会给回一堆。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    want = name.split(".")
    out: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                qual = prefix + [child.name]
                if qual[-len(want):] == want:
                    # 装饰器在 def 上面，start 要往上收
                    start = min([child.lineno]
                                + [d.lineno for d in child.decorator_list])
                    out.append((".".join(qual), start,
                                child.end_lineno or child.lineno))
                    continue                   # 命中之后不再往里钻
                walk(child, qual)
            else:
                walk(child, prefix)

    walk(tree, [])
    return out


def _indent_spans(lines: list[str], name: str) -> list[tuple[str, int, int]]:
    """缩进兜底：ast 解析不了时用。定义体 = 缩进比声明行更深的连续区段。"""
    leaf = name.split(".")[-1]
    decl = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+"
                      + re.escape(leaf) + r"\s*[(:]")
    out: list[tuple[str, int, int]] = []
    for i, ln in enumerate(lines):
        m = decl.match(ln)
        if m is None:
            continue
        indent = len(m.group(1))
        start = i
        while start > 0 and lines[start - 1].lstrip().startswith("@"):
            start -= 1
        end = len(lines) - 1
        for j in range(i + 1, len(lines)):
            if not lines[j].strip():
                continue
            if len(lines[j]) - len(lines[j].lstrip()) <= indent:
                end = j - 1
                break
        while end > i and not lines[end].strip():
            end -= 1
        out.append((leaf, start + 1, end + 1))
    return out


def _brace_spans(lines: list[str], name: str) -> list[tuple[str, int, int]]:
    """花括号语言：声明行 + 配对的 `}`。Maven 适配器是一等公民。

    括号计数不认字符串与注释里的括号 —— 那需要一个真正的词法器，代价远高于
    这里的收益。代价是极少数情况下右边界偏大，而偏大只是多读几行，偏小才会
    让模型据一段被切断的代码去改。宁可多给。
    """
    leaf = name.split(".")[-1]
    esc = re.escape(leaf)
    call = re.compile(r"(^|[^A-Za-z0-9_.])" + esc + r"\s*\(")
    kw = re.compile(r"\b(class|interface|enum|record|struct)\s+" + esc
                    + r"(\s|\{|<|$)")
    out: list[tuple[str, int, int]] = []
    for i, ln in enumerate(lines):
        if not (kw.search(ln) or (call.search(ln) and not _NOT_DECL.match(ln))):
            continue
        # 声明行本身带 `{`，或者 `{` 单独占下一行（K&R 之外的写法）
        opens = i if "{" in ln else -1
        if opens == -1:
            for j in range(i + 1, min(i + 3, len(lines))):
                if lines[j].strip() == "{":
                    opens = j
                    break
        if opens == -1:
            continue
        depth = 0
        end = -1
        for j in range(opens, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0:
                end = j
                break
        if end != -1:
            out.append((leaf, i + 1, end + 1))
    return out


def _spans(path: Path, name: str) -> list[tuple[str, int, int]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = src.splitlines()
    if path.suffix in _PY:
        return _py_spans(src, name) or _indent_spans(lines, name)
    return _brace_spans(lines, name) or _indent_spans(lines, name)


class ReadSymbolTool(Tool):
    name = "read_symbol"
    description = (
        "按名字读一个函数/类/方法的完整定义，返回带行号的源码 —— "
        "不用先 grep 行号再猜 read_file 的窗口。"
        "不给 path 就在整个工作区里找；方法可用 `类名.方法名`。")

    class Params(BaseModel):
        name: str = Field(description="函数/类/方法名，可写成 `Cart.total`")
        path: str = Field(default="", description="限定文件；不填则全仓库搜")

    def __init__(self, sandbox: Sandbox, timeout: float = 30.0,
                 max_chars: int = 8000, max_hits: int = 5) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._max_chars = max_chars
        self._max_hits = max_hits

    async def run(self, params: "ReadSymbolTool.Params") -> str:
        root = Path(self._sandbox.workspace)
        try:
            files = ([self._fenced(params.path)] if params.path
                     else await self._search(params.name))
        except SandboxError as e:
            raise ToolError(str(e)) from None

        hits: list[tuple[int, str, str, int, int, list[str]]] = []
        for rel in files:
            p = root / rel
            if p.suffix.lower() in _SKIP_SUFFIX:
                continue
            spans = _spans(p, params.name)
            if not spans:
                continue
            lines = p.read_text(encoding="utf-8",
                                errors="replace").splitlines()
            for qual, a, b in spans:
                # 限定名与查询**完全相同**的排在前面：查 `Cart.total` 时，
                # 别处一个裸 `total` 不该把真正的那个挤掉。
                hits.append((0 if qual == params.name else 1,
                             rel, qual, a, b, lines))
        hits.sort(key=lambda h: h[0])
        found = [h[1:] for h in hits[:self._max_hits]]

        if not found:
            where = f"在 {params.path} 里" if params.path else "在工作区里"
            return (f"{where}没找到名为 `{params.name}` 的函数或类。\n"
                    f"确认一下拼写；或者用 grep 搜 `{params.name}` "
                    "看它到底出现在哪 —— 它可能是变量、也可能来自依赖库。")

        blocks: list[str] = []
        used = 0
        for rel, qual, a, b, lines in found:
            head = f"── {rel}  第 {a}-{b} 行  {qual}"
            body: list[str] = []
            cut = 0
            for n in range(a, min(b, len(lines)) + 1):
                row = f"{n:>6}\t{lines[n - 1]}"
                if used + len(row) + 1 > self._max_chars:
                    cut = n
                    break
                body.append(row)
                used += len(row) + 1
            block = head + "\n" + "\n".join(body)
            if cut:
                # 可操作的截断：说清楚从哪续读、用哪个工具。
                # 「已截断」而不给出路，会把模型逼进重读同一段的死循环 ——
                # read_file 吃过这个亏，这里不再吃第二次。
                block += (f"\n…（已截断，只给到第 {cut - 1} 行。续读："
                          f'read_file path="{rel}" offset={cut}）')
                blocks.append(block)
                break
            blocks.append(block)

        note = ("\n\n（同名的有多处，上面全列出来了 —— 先确认要改的是哪一个。）"
                if len(blocks) > 1 else "")
        return "\n\n".join(blocks) + note

    def _fenced(self, rel: str) -> str:
        resolve_in_workspace(self._sandbox.workspace, rel)
        return rel

    async def _search(self, name: str) -> list[str]:
        """用 git grep 找**定义**出现在哪些文件里。

        `--untracked` 是必须的：修复过程中新建的文件还没进索引，漏掉它们会
        让「刚写的那个函数找不到」——而那正是模型最需要回头看它的时候。
        """
        leaf = re.escape(name.split(".")[-1])
        pats = [
            # def / class / interface / enum …：关键字带头的定义
            r"(def|class|interface|enum|record|struct)[[:space:]]+"
            + leaf + r"[^A-Za-z0-9_]",
            # 花括号语言的方法声明：`... name(...) ... {`
            leaf + r"[[:space:]]*\(.*\)[^;]*\{",
        ]
        out: list[str] = []
        for pat in pats:
            res = await self._sandbox.exec(
                ["git", "grep", "-l", "-I", "-E", "--untracked", pat],
                self._timeout)
            if res.exit_code not in (0, 1):
                continue
            for rel in res.stdout.splitlines():
                if rel and rel not in out:
                    out.append(rel)
        return out
