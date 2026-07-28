import ast
from pathlib import Path

from aifix.eval.mutate import Mutation, mutations

REPO_ROOT = Path(__file__).resolve().parents[1]


def _one_line_diff(src: str, mutated: str) -> list[tuple[str, str]]:
    return [(x, y) for x, y in zip(src.splitlines(), mutated.splitlines())
            if x != y]


def _changed_lines(src: str, muts) -> set[str]:
    """收集所有变异**实际改出来的**那一行，未改动的行不进来。"""
    return {y for m in muts for _, y in _one_line_diff(src, m.source)}


def test_operators_produce_single_point_changes():
    """每个变异只动一处 —— 多处变异让 ground truth 不再是单点。"""
    src = "def f(a, b):\n    if a < b:\n        return a + 1\n    return True\n"
    muts = list(mutations(src))
    assert len(muts) >= 4
    for m in muts:
        # 与原文恰好差一处：逐行比对只有一行不同
        diff_lines = _one_line_diff(src, m.source)
        assert len(diff_lines) == 1, (m.description, diff_lines)
        # 行数不变 —— 变异是行内替换，不许吞行或加行
        assert len(m.source.splitlines()) == len(src.splitlines())


def test_comparison_flip():
    src = "def f(a, b):\n    return a < b\n"
    got = {m.source for m in mutations(src)}
    assert "def f(a, b):\n    return a <= b\n" in got


def test_mutated_source_still_parses():
    """拿本仓库的真实文件当输入 —— 手写片段暴露不出定位 bug。"""
    src = (REPO_ROOT / "src/aifix/eval/score.py").read_text(encoding="utf-8")
    muts = list(mutations(src))
    # 真实文件里有比较、算术、and、整数常量，四类算子都该命中
    assert len(muts) >= 8
    for m in muts:
        ast.parse(m.source)      # 语法坏掉的变异是废品，不是任务
        assert len(_one_line_diff(src, m.source)) == 1, m.description


def test_all_operator_families_covered():
    """算子表里的每一条都要真的生效，缺一条就是少一整类冒烟任务。"""
    src = (
        "def f(a, b, c, d):\n"
        "    x = a > b\n"
        "    y = a == b\n"
        "    z = a * b\n"
        "    w = a - b\n"
        "    v = c and d\n"
        "    u = c or d\n"
        "    t = a // b\n"
        "    return x\n"
    )
    # 每个算子恰好改出一行，全集比对：多一条少一条都算失败
    assert _changed_lines(src, mutations(src)) == {
        "    x = a >= b",
        "    y = a != b",
        "    z = a // b",
        "    w = a + b",
        "    v = c or d",
        "    u = c and d",
        "    t = a * b",
    }


def test_bool_and_int_constants():
    src = "def f():\n    a = True\n    b = 41\n    return a, b\n"
    # bool 是 int 的子类：True 只能翻成 False，绝不能被 n→n+1 变成 2
    assert _changed_lines(src, mutations(src)) == {"    a = False", "    b = 42"}


def test_skips_strings_annotations_and_fstrings():
    """字符串字面量、类型注解、f-string 内部一律不动 —— 见模块内的取舍说明。"""
    src = (
        "def f(x: int = 3, s: str = 'a<b') -> bool:\n"
        "    msg = f'{x + 1} 个'\n"
        "    n: int = 7\n"
        "    return True\n"
    )
    # 只有 `return True` 那一行该被动
    assert _changed_lines(src, mutations(src)) == {"    return False"}


def test_multiline_operator_is_skipped():
    """跨行的运算符定位不可靠，直接跳过 —— 宁可少产任务，不可产错任务。"""
    src = "def f(a, b):\n    return (a\n            < b)\n"
    assert list(mutations(src)) == []


def test_order_is_stable_and_sorted():
    """任务 9 的 --seed 采样要靠这个顺序才可复现。"""
    src = "def f(a, b):\n    if a < b:\n        return a + 1\n    return True\n"
    first = [(m.lineno, m.description, m.source) for m in mutations(src)]
    second = [(m.lineno, m.description, m.source) for m in mutations(src)]
    assert first == second
    assert [m[0] for m in first] == sorted(m[0] for m in first)
    assert [m[0] for m in first] == [2, 3, 3, 4]


def test_description_names_the_change():
    """description 会进任务 id 和报告，必须一眼看懂改了什么。"""
    src = "def f(a, b):\n    return a < b\n"
    m = next(iter(mutations(src)))
    assert m.description == "比较运算符 < → <="
    assert m.lineno == 2


def test_mutation_is_frozen():
    src = "def f(a, b):\n    return a < b\n"
    m = next(iter(mutations(src)))
    assert isinstance(m, Mutation)
    try:
        m.lineno = 99                                   # type: ignore[misc]
    except Exception as exc:                            # frozen dataclass
        assert type(exc).__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("Mutation 应当是 frozen 的")


def test_syntactically_broken_source_yields_nothing():
    assert list(mutations("def f(:\n")) == []
