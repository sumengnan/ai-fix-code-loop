"""人造变异（C 类任务）的 AST 算子层。

**这是冒烟集，不是基准。** 变异出来的 bug 分布跟真实 bug 不是一回事：它便宜、
确定、可以任意规模，用途是验证「挖任务 → 跑 agent → 打分」这条链路本身通不通。
拿它的成功率去跨模型比高低是过度解读 —— 一个把 `<` 改回 `<=` 的单点变异，
和 mined 那一类从 git history 里挖出来的真实红转绿 commit，难度不在一个量级。
两类任务的成绩必须分开报（见 task.Task.origin）。

本模块只负责**产出变异**：给一份源码，吐出一串「只动一处」的候选。验证变异是否
真的把测试弄红、以及落成 Task，是任务集构建那一层的事，不在这里。

## 为什么是文本替换而不是 ast.unparse

用 `ast.unparse` 重新生成整个文件会把格式、空行、注释全部抹掉，产出的 diff 是
「整文件重写」：既不像真实 bug（模型看到的上下文全变了），也会当场撞上本项目的
巨型 diff 守卫（max_diff_lines 默认 300）。所以 AST 只负责**定位**，替换在原文
那一行上按字节切片做，其余字节一个不动。

## 哪些 Constant 会被变异（取舍）

只在这些位置上的 Constant 施加：`ast.Compare` / `ast.BinOp` / `ast.BoolOp` 的
操作数，以及 `Return` / `Assign` 的右值。这条线是故意划窄的：

- 不动函数默认值（`def f(x=3)`）和 `AnnAssign`（`n: int = 7`）—— 这两处的常量
  多半是接口契约，改了往往一大片测试同时变红，落不出「单点」的 ground truth。
- 不进 f-string 内部（整棵 JoinedStr 子树都排除）—— 一来简报明确要求不动，
  二来 3.11 上 f-string 内层表达式的 col_offset 不可靠，按它切片会切错位置。
- 字符串字面量天然不在算子表里（只处理 bool 和 int），不需要额外规则。

宁可少产任务，不可产错任务：定位一旦不可靠，变异出来的就不是「一个 bug」，
而是一份语义不明的脏 diff。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterator

# 算子表。value 是 (原文, 变成什么)，原文用来在两个操作数之间定位。
_CMP_OPS: dict[type, tuple[str, str]] = {
    ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="), ast.GtE: (">=", ">"),
    ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="),
}
_BIN_OPS: dict[type, tuple[str, str]] = {
    ast.Add: ("+", "-"), ast.Sub: ("-", "+"),
    ast.Mult: ("*", "//"), ast.FloorDiv: ("//", "*"),
}
_BOOL_OPS: dict[type, tuple[str, str]] = {
    ast.And: ("and", "or"), ast.Or: ("or", "and"),
}


@dataclass(frozen=True)
class Mutation:
    source: str          # 变异后的完整文件内容
    lineno: int
    description: str     # 形如 "比较运算符 < → <="


# 一处待施加的替换：改哪一行、行内哪个字节区间、换成什么、怎么描述。
# 字节区间而不是字符区间 —— 见 _splice 的说明。
_Edit = tuple[int, int, int, str, str]


def _splice(lines: list[str], lineno: int, start: int, end: int,
            new_text: str) -> str:
    """把第 lineno 行的 [start, end) 字节替换成 new_text，其余原样拼回。

    按**字节**而不是字符切片：AST 的 col_offset 是 UTF-8 字节偏移，本仓库源码
    里中文注释和中文字符串遍地都是，同一行里只要运算符前面有一个中文字符，
    按字符切片就会切偏。
    """
    raw = lines[lineno - 1].encode("utf-8")
    patched = (raw[:start] + new_text.encode("utf-8") + raw[end:]).decode("utf-8")
    return "".join(lines[:lineno - 1] + [patched] + lines[lineno:])


def _slice(lines: list[str], lineno: int, start: int, end: int) -> str:
    return lines[lineno - 1].encode("utf-8")[start:end].decode("utf-8")


def _operator_edit(lines: list[str], left: ast.AST, right: ast.AST,
                   old: str, new: str, label: str) -> _Edit | None:
    """在左右操作数之间那一段里定位运算符。

    运算符节点（ast.Lt / ast.Add / ast.And 之类）在 Python 3.8+ 是单例，
    **没有** lineno/col_offset，拿不到位置。只能退而求其次：左操作数的
    end_col_offset 到右操作数的 col_offset 之间，除了运算符就只剩空白、
    括号和续行符，在这一段里找运算符文本是安全的。

    跨行的直接放弃（`a\\n  < b` 这种）：定位要跨两行拼接，而单点变异本来就
    不缺候选，不值得为这点边角复杂化。
    """
    if left.end_lineno != right.lineno:
        return None
    start = left.end_col_offset
    end = right.col_offset
    if start is None or end is None or end <= start:
        return None
    segment = _slice(lines, right.lineno, start, end)
    idx = segment.encode("utf-8").find(old.encode("utf-8"))
    if idx < 0:
        return None
    at = start + idx
    return (right.lineno, at, at + len(old.encode("utf-8")), new,
            f"{label} {old} → {new}")


def _constant_edit(lines: list[str], node: ast.Constant) -> _Edit | None:
    value = node.value
    if isinstance(value, bool):
        # bool 必须排在 int 前面判断：bool 是 int 的子类，交换顺序的话
        # True 会走到 n→n+1 那条，被改成 2
        new_text = "False" if value else "True"
        label = "布尔常量"
    elif type(value) is int:
        new_text = str(value + 1)
        label = "整数常量"
    else:
        return None
    if node.end_lineno != node.lineno or node.col_offset is None:
        return None
    old_text = _slice(lines, node.lineno, node.col_offset, node.end_col_offset)
    return (node.lineno, node.col_offset, node.end_col_offset, new_text,
            f"{label} {old_text} → {new_text}")


def _fstring_node_ids(tree: ast.AST) -> set[int]:
    banned: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            banned.update(id(inner) for inner in ast.walk(node))
    return banned


def _collect(tree: ast.AST, lines: list[str]) -> list[_Edit]:
    banned = _fstring_node_ids(tree)
    edits: list[_Edit] = []
    seen_constants: set[int] = set()

    def take_constant(node: ast.AST | None) -> None:
        if not isinstance(node, ast.Constant) or id(node) in banned:
            return
        if id(node) in seen_constants:
            return
        seen_constants.add(id(node))
        edit = _constant_edit(lines, node)
        if edit is not None:
            edits.append(edit)

    for node in ast.walk(tree):
        if id(node) in banned:
            continue
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for i, op in enumerate(node.ops):
                pair = _CMP_OPS.get(type(op))
                if pair is None:          # is / in / not in：不在算子表里
                    continue
                edit = _operator_edit(lines, operands[i], operands[i + 1],
                                      *pair, "比较运算符")
                if edit is not None:
                    edits.append(edit)
            for operand in operands:
                take_constant(operand)
        elif isinstance(node, ast.BinOp):
            pair = _BIN_OPS.get(type(node.op))
            if pair is not None:
                edit = _operator_edit(lines, node.left, node.right, *pair,
                                      "算术运算符")
                if edit is not None:
                    edits.append(edit)
            take_constant(node.left)
            take_constant(node.right)
        elif isinstance(node, ast.BoolOp):
            pair = _BOOL_OPS.get(type(node.op))
            # 一个 BoolOp 可以带多个操作数（`a and b and c` 只有一个 And
            # 节点），每个间隔各产一个变异
            for left, right in zip(node.values, node.values[1:]):
                if pair is not None:
                    edit = _operator_edit(lines, left, right, *pair,
                                          "逻辑运算符")
                    if edit is not None:
                        edits.append(edit)
            for value in node.values:
                take_constant(value)
        elif isinstance(node, (ast.Return, ast.Assign)):
            take_constant(node.value)
    return edits


def mutations(source: str) -> Iterator[Mutation]:
    """对一份源码逐点施加变异，每次只动一处。

    产出顺序按 (行号, 列号) 稳定排序：同一份输入每次给出同一串变异，
    否则任务集构建那一层的 --seed 采样就没有可复现性可言。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 输入就是坏的，谈不上变异
        return
    lines = source.splitlines(keepends=True)
    for lineno, start, end, new_text, description in sorted(
            _collect(tree, lines), key=lambda e: (e[0], e[1], e[4])):
        mutated = _splice(lines, lineno, start, end, new_text)
        try:
            ast.parse(mutated)
        except SyntaxError:
            # 语法坏掉的变异是废品不是任务：agent 面对的应该是一个能跑起来
            # 但结果不对的程序，不是一份编译不过的文件
            continue
        yield Mutation(source=mutated, lineno=lineno, description=description)
