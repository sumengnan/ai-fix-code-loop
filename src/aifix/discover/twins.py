"""找「两处实现同一件事」的候选 —— 纯 AST，零模型调用。

它们对同一输入给不同答案时，那就是一个不需要任何人说出期望值的缺陷：
判据是**两边不一致**本身。这让它成为少数几个能自己造 oracle 的发现源。

判据用两个维度，缺一个都不成立：

  ① 分支在同一组字符串字面量上  —— 在处理同一个域
  ② 函数名有共同词根            —— 在做同一件事

只有 ① 时，「渲染题目」会和「判分」配成一对：确实共享题型枚举，但不是两处
实现同一件事。实测一个真实仓库：只有 ① 是 18 对，两个维度一起是 2 对，
而其中一对正是一个真 bug。

**这一层只出候选，不下结论。** 候选要靠「跑一遍看结果一不一致」证伪，一致的
静默丢弃 —— 一个不能被便宜地证伪的候选就是噪音。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# 分派键的最短长度。`x == ""` / `x == "y"` 到处都是，算进来这一列会恒亮，
# 而恒亮的信号和没有这条信号是一个结果。
_MIN_LITERAL = 2
# 词根的最短长度，与上一条同理（`id` / `to` 配不出信息）。
_MIN_ROOT = 3
# 到处都是的动词。拿它们配对等于随机配。
_STOP_ROOTS = frozenset({
    "get", "set", "make", "build", "run", "do", "handle", "process", "call",
    "new", "add", "put", "the", "and", "for", "with", "from", "into",
})
_SKIP_PARTS = ("__pycache__", ".venv", "node_modules", "site-packages")


@dataclass(frozen=True)
class Site:
    """一个候选点：路径（相对仓库根）+ 函数名 + 行号。"""
    path: str
    name: str
    lineno: int


@dataclass(frozen=True)
class Twin:
    """一对候选。`shared_*` 是配对的依据，报告与下一层都要用它解释「为什么是这两个」。"""
    a: Site
    b: Site
    shared_literals: frozenset[str]
    shared_roots: frozenset[str]

    @property
    def score(self) -> int:
        """词根权重更高：同域容易撞（一个枚举被十个函数用），同名才说明同职责。"""
        return len(self.shared_literals) + 3 * len(self.shared_roots)


def branch_literals(fn: ast.AST) -> frozenset[str]:
    """这个函数在哪些字符串字面量上**做分支判断**。

    只收比较与成员判断里的字符串常量：赋值、返回、拼接里的字面量不是分派
    依据，收进来会把信号淹掉（实测里那类字面量比分派键多一个数量级）。
    """
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare):
            for side in (n.left, *n.comparators):
                _collect(side, out)
        elif isinstance(n, ast.match_case):
            for p in ast.walk(n.pattern):
                if (isinstance(p, ast.MatchValue)
                        and isinstance(p.value, ast.Constant)):
                    _collect(p.value, out)
    return frozenset(x for x in out if len(x) >= _MIN_LITERAL)


def _collect(node: ast.AST, out: set[str]) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.add(node.value)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.add(e.value)


def name_roots(name: str) -> frozenset[str]:
    """`grade_objective` / `gradeObjective` → `{grade, objective}`。"""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return frozenset(t for t in s.split("_")
                     if len(t) >= _MIN_ROOT and t not in _STOP_ROOTS)


def _is_product_file(p: Path) -> bool:
    """测试文件排除掉：它们当然会有与产品代码同名同枚举的东西。

    判据刻意宽松（路径里任一段以 test 开头）—— 这一层宁可少给候选，也不要
    把一整片测试代码配进来，那会让候选清单直接失去可读性。
    """
    parts = p.parts
    return not any(x in parts for x in _SKIP_PARTS) and not any(
        seg.startswith("test") for seg in parts)


def _functions(root: Path):
    for p in sorted(root.rglob("*.py")):
        if not _is_product_file(p.relative_to(root)):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # 一个坏文件不该让整次扫描什么都不产出
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield p.relative_to(root).as_posix(), n


def find_twins(root: Path | str, min_shared: int = 3) -> list[Twin]:
    """扫描仓库，返回按分数降序的候选对。

    `min_shared`：两个函数至少要共享几个分派键。低于 3 时「都判过 `x == "a"`」
    也会配对，那不说明它们同域。

    同一个文件里的两个函数不配对：那多半是重载或分步，不是「两处实现」。

    结果**排过序且可复现**：集合的迭代顺序不可复现，而两次扫同一个仓库给出
    不同的清单会让这层的产出没法被引用。
    """
    r = Path(root)
    items: list[tuple[Site, frozenset[str], frozenset[str]]] = []
    for rel, fn in _functions(r):
        lits = branch_literals(fn)
        if len(lits) >= min_shared:
            items.append((Site(rel, fn.name, fn.lineno), lits,
                          name_roots(fn.name)))

    out: list[Twin] = []
    for i, (sa, la, ra) in enumerate(items):
        for sb, lb, rb in items[i + 1:]:
            if sa.path == sb.path:
                continue
            shared_lit, shared_root = la & lb, ra & rb
            if len(shared_lit) >= min_shared and shared_root:
                out.append(Twin(a=sa, b=sb, shared_literals=shared_lit,
                                shared_roots=shared_root))
    return sorted(out, key=lambda t: (-t.score, t.a.path, t.a.lineno,
                                      t.b.path, t.b.lineno))
