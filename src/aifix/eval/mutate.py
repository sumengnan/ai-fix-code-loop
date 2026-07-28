"""人造变异（C 类任务）的 AST 算子层。

**这是冒烟集，不是基准。** 变异出来的 bug 分布跟真实 bug 不是一回事：它便宜、
确定、可以任意规模，用途是验证「挖任务 → 跑 agent → 打分」这条链路本身通不通。
拿它的成功率去跨模型比高低是过度解读 —— 一个把 `<` 改回 `<=` 的单点变异，
和 mined 那一类从 git history 里挖出来的真实红转绿 commit，难度不在一个量级。
两类任务的成绩必须分开报（见 task.Task.origin）。

模块分两层。下半层 `mutations(source)` 是纯函数：给一份源码，吐出一串「只动
一处」的候选，不碰磁盘也不跑测试。上半层 `mutate_tasks` 才去克隆仓库、逐个施加
候选、跑测试、把真的弄红了的那些落成 Task。**一个变异不算任务，一个被验证过
确实红了的变异才算** —— 「生成了 N 条记录」证明不了任何事。

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
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from ..adapters.pytest_adapter import PytestAdapter
from ..nodes.baseline import run_full_suite, run_scoped
from .mine import split_paths
from .task import Task
from .workspace import materialize

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


# ---------------------------------------------------------------- 任务产出层

def _git(cwd: str | Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()}")
    return res.stdout


def _test_index(ran: frozenset[str]) -> dict[str, list[str]]:
    """从一次全量的 ran 集合建「测试文件 → 用例」索引。

    不另外解析 XML：全绿时 failures 是空的，ran 里是形如
    `tests/test_x.py::test_a` 的 id，按 `::` 前缀分组就是索引。收集失败发出的
    文件级 id（不带 `::`）在这里恰好自成一组，键仍是文件路径，行为一致。
    """
    index: dict[str, list[str]] = {}
    for test_id in sorted(ran):
        index.setdefault(test_id.split("::", 1)[0], []).append(test_id)
    return index


def _scope_for(rel: str, index: dict[str, list[str]],
               scope: str) -> list[str] | None:
    """这个源文件的变异要跑哪些测试；None 表示跑全量。

    `smart` 按**词干**匹配：源文件 `src/aifix/eval/mine.py` 的 stem 是 `mine`，
    候选是文件名里含 `mine` 的测试文件（`tests/test_eval_mine.py`）。这是个
    命名约定上的启发式，**会漏** —— 被变异的文件其实由别处的测试覆盖时匹配
    不到，返回空列表，调用方跳过这个文件。

    **漏是安全的**：少一个候选而已，不会产出假任务。反过来（跑得太窄，把
    「其实没弄红任何测试」的变异当成弄红了）才会造出无人能通过的任务，而
    那不可能发生：判据是「跑到的这些测试里有没有新红」，跑不到就没有新红。
    """
    if scope == "full":
        return None
    if scope != "smart":
        raise ValueError(f"未知的测试范围 {scope!r}，只支持 smart / full")
    stem = PurePosixPath(rel).stem
    return [f for f in sorted(index) if stem in PurePosixPath(f).name]


async def _run(tree: Path, adapter: PytestAdapter,
               scope_files: list[str] | None):
    if scope_files is None:
        return await run_full_suite(tree, adapter, require_report=True)
    return await run_scoped(tree, adapter, scope_files, require_report=True)


async def mutate_tasks(repo: str, adapter: PytestAdapter, max_tasks: int = 10,
                       max_new_failures: int = 5, scope: str = "smart",
                       seed: int = 0, workdir: Path | None = None,
                       on_progress=None) -> list[Task]:
    """在一个全绿仓库上生成人造变异任务（C 类）。

    HEAD 必须全绿，否则直接抛：在一个本来就红的仓库上做变异，分不清红是变异
    造成的还是本来就有的 —— 「新失败」这个判据是拿变异后的失败集合直接当答案
    的，基线不干净它就没有意义。

    收下一个变异的条件是新失败数落在 `[1, max_new_failures]`：一个都没红说明
    这个变异没被任何测试覆盖到，不构成任务；红了一大片的变异太显眼（模型看
    一眼失败面就知道要往哪改），也违反「单点缺陷」这个前提 —— ground truth
    只有一个文件，却弄红了半个套件，那更像是接口契约被改了。

    on_progress(rel_path, n, error)：沿用 mine_tasks 的约定，n≥0 是这个源文件
    产出的任务数（0 是正常结果：没有候选、或候选全被丢弃、或 smart 范围没
    匹配到测试文件）。
    """
    workdir = Path(workdir or Path(repo) / ".aifix" / "mutate")
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    name = Path(repo).name
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = workdir / "tree"
    tasks: list[Task] = []

    try:
        # 变异只落在这份克隆里，源仓库一个字节都不动 —— 产出的 Task 带的是
        # unified diff，materialize 时才现场施加
        materialize(repo, head, head, [], tree)
        green = await run_full_suite(tree, adapter, require_report=True)
        if green.ids:
            raise RuntimeError(
                f"仓库 HEAD（{head[:8]}）不是全绿，无法做变异："
                f"{len(green.ids)} 个用例已经在红"
                f"（{'、'.join(sorted(green.ids)[:3])}）。"
                "基线不干净时，变异后的新失败分不清是变异造成的还是本来就有的")
        if not green.ran:
            # 报告存在但一个用例都没跑到（收集整轮中止）。这不是「全绿」，
            # 而是压根没跑 —— 放行的话下面每个变异的 scoped 也跑不到东西，
            # 整轮静默产出 0 个任务，与「这些变异都没弄红测试」无法区分
            raise RuntimeError(
                f"仓库 HEAD（{head[:8]}）的全量一个用例都没跑到"
                f"（worktree={tree}），本次结果不可信")
        index = _test_index(green.ran)

        paths = [p for p in _git(tree, "ls-files", "--", "*.py").split("\n")
                 if p.strip()]
        _tests, sources = split_paths(paths, adapter.test_dirs())
        # seed 决定文件顺序与每个文件内候选的顺序：max_tasks 一截断，取到哪
        # 几个变异就全看这个顺序，不定死就没有可复现的任务集可言
        rng = random.Random(seed)
        rng.shuffle(sources)

        for rel in sources:
            if len(tasks) >= max_tasks:
                break
            scope_files = _scope_for(rel, index, scope)
            if scope_files is not None and not scope_files:
                if on_progress:
                    on_progress(rel, 0, None)
                continue
            path = tree / rel
            original = path.read_text(encoding="utf-8")
            candidates = list(mutations(original))
            rng.shuffle(candidates)
            n = 0
            for m in candidates:
                if len(tasks) >= max_tasks:
                    break
                path.write_text(m.source, encoding="utf-8")
                try:
                    fs = await _run(tree, adapter, scope_files)
                    # 趁变异还在工作区里取 diff。每个 Mutation.source 都是从
                    # 原文整体重算的，所以工作区里永远只有一处改动
                    diff = _git(tree, "diff", "--", rel)
                except RuntimeError:
                    # 报告缺失 / git 失败：这一个候选作废，不带走整轮变异
                    continue
                finally:
                    # 还原必须无条件执行 —— 上一个变异留在工作区里，下一个
                    # 候选的「新失败」就分不清是谁造成的了
                    path.write_text(original, encoding="utf-8")
                new = sorted(fs.ids)
                if not (1 <= len(new) <= max_new_failures) or not diff.strip():
                    continue
                target = new[0]
                tasks.append(Task(
                    task_id=f"{name}@mut::{rel}:{m.lineno}::{target}",
                    repo=str(Path(repo).resolve()), commit=head,
                    base_commit=head, test_files=[], target_test=target,
                    gold_files=[rel], adapter=adapter.name,
                    origin="mutated", mutation_diff=diff))
                n += 1
            if on_progress:
                on_progress(rel, n, None)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return tasks
