"""变形复跑：这个补丁是只对那一个例子成立，还是真的修好了。

复现测试只有一个样本点，而 fixer 的停止条件就是「它绿了」。贴着那个样本写的
补丁和真修好的补丁，在判定眼里一模一样。

做法是机械地扰动测试里的字面量再跑一遍。扰动**不经模型**——它不需要知道正确
答案，只需要知道「这个变换不该改变结论」。

误伤是这里最贵的错误，所以每个扰动都带对照组（见 `diverging_mutations`）。
"""
from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters.base import FailureSet


@dataclass(frozen=True)
class Mutation:
    """对测试源码的一处机械改写：把 [start, end) 换成 text。"""
    start: int
    end: int
    text: str
    label: str


@dataclass(frozen=True)
class Divergence:
    """一处「变形之后补丁就不成立了」。"""
    label: str


@dataclass
class Metamorphic:
    """三种「没话说」必须分得开，只有第一种是好消息。

    diverged 非空 —— 补丁只对原样本成立
    diverged 空且 checked>0 —— 查过，扛住了扰动
    checked 为 0 —— 根本没查（没有可扰动的字面量 / 这一层没开）
    """
    diverged: list[Divergence] = field(default_factory=list)
    checked: int = 0
    # 扰动把测试本身搞坏了（对照组不红），这一个不作数
    discarded: int = 0


def _line_starts(source: str) -> list[int]:
    offs, pos = [0], 0
    for line in source.splitlines(keepends=True):
        pos += len(line)
        offs.append(pos)
    return offs


def plan_mutations(source: str) -> list[Mutation]:
    """枚举可做的扰动。目前只有一种：把全常量的列表字面量整体右旋一位。

    只取全常量的列表：元素里有表达式时换序还会改变求值顺序，那就不只是
    「换个序」了，测试红了也说明不了补丁的问题。

    按原文片段拼回去，不用 `ast.unparse` 重新渲染：后者会顺手改写引号、
    去掉注释，把「只换了序」变成一次谁也说不清的改写。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    starts = _line_starts(source)

    def _off(lineno: int, col: int) -> int:
        # ast 的 col_offset 是 utf-8 字节偏移，而这里按字符切片
        return starts[lineno - 1] + len(
            source[starts[lineno - 1]:].encode()[:col].decode(errors="ignore"))

    out: list[Mutation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        if not all(isinstance(e, ast.Constant) for e in node.elts):
            continue
        segs = [ast.get_source_segment(source, e) for e in node.elts]
        if any(s is None for s in segs):
            continue
        start = _off(node.lineno, node.col_offset)
        end = _off(node.end_lineno, node.end_col_offset)
        text = "[" + ", ".join(segs[-1:] + segs[:-1]) + "]"
        out.append(Mutation(start=start, end=end, text=text,
                            label=f"第 {node.lineno} 行 {source[start:end]} → {text}"))
    return sorted(out, key=lambda m: m.start)


def apply_mutation(source: str, m: Mutation) -> str:
    return source[:m.start] + m.text + source[m.end:]


async def diverging_mutations(
    worktree: Path,
    test_file: str,
    product: dict[str, tuple[str | None, str | None]],
    target: str,
    rerun: Callable[[list[str]], Awaitable[FailureSet]],
    max_mutations: int,
) -> Metamorphic:
    """报出「扰动一下补丁就不成立了」的那些变形。

    `product`：路径 → (HEAD 内容, 打了补丁的内容)。对照组要把产品代码退回
    HEAD，跑完再放回来。

    每个扰动两问，顺序刻意如此（好路径只花一次重跑）：

    1. 打了补丁 + 变形后的测试 → 绿？补丁扛住了这个扰动，到此为止。
    2. 红的话再问对照组：退回 HEAD 的产品代码，同一个变形测试还红不红。
       仍然红说明这个变形**依然测得到那个缺陷**，于是第 1 步的红是补丁的问题；
       变绿则说明变形把测试本身搞坏了（多半动到的是期望值而不是输入），
       这一个丢弃、不出声。

    **保证工作区逐字还原**，包括异常路径。差一个字节，随后 commit 的就不是被
    验证过的那个补丁，而这件事没有任何一处会出声。
    """
    path = worktree / test_file
    if not path.is_file():
        return Metamorphic()
    source = path.read_text(encoding="utf-8")
    mutations = plan_mutations(source)[:max_mutations]
    if not mutations:
        return Metamorphic()

    def _write(p: str, text: str | None) -> None:
        f = worktree / p
        if text is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")

    out = Metamorphic()
    try:
        for m in mutations:
            path.write_text(apply_mutation(source, m), encoding="utf-8")
            out.checked += 1
            if target not in (await rerun([target])).ids:
                continue                        # 扛住了
            for p, (head, _patched) in product.items():
                _write(p, head)
            try:
                still_red = target in (await rerun([target])).ids
            finally:
                for p, (_head, patched) in product.items():
                    _write(p, patched)
            if still_red:
                out.diverged.append(Divergence(label=m.label))
            else:
                out.discarded += 1
    finally:
        path.write_text(source, encoding="utf-8")
    return out
