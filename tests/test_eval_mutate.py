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


import subprocess

import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.eval.mutate import mutate_tasks
from aifix.eval.workspace import prepare_task_repo
from aifix.nodes.baseline import run_full_suite

_PYTEST_INI = "[pytest]\npythonpath = .\n"

# 全绿夹具。calc.py 上恰好三个变异候选，每个只弄红一个用例：
#   `+` → `-`   → test_add 红
#   `>` → `>=`  → test_is_big 红（is_big(10) 变 True）
#   `10` → `11` → test_is_big 红（is_big(11) 变 False）
# 用例只有两个 —— 「产出的任务真的红」那条测试要为每个任务真跑一次全量，
# 套件大一点这个测试就慢得没法进 CI
_GREEN_SRC = '''def add(a, b):
    return a + b


def is_big(n):
    return n > 10
'''

_GREEN_TEST = '''from calc import add, is_big


def test_add():
    assert add(2, 3) == 5


def test_is_big():
    assert is_big(11) is True
    assert is_big(10) is False
'''


def _init_repo(repo: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-q", "-m", "init"],
                   cwd=repo, check=True)
    return repo


def _make_green_repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "green", {
        "pytest.ini": _PYTEST_INI,
        "calc.py": _GREEN_SRC,
        "tests/test_calc.py": _GREEN_TEST,
    })


def _make_red_repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "red", {
        "pytest.ini": _PYTEST_INI,
        "calc.py": "def add(a, b):\n    return a - b\n",
        "tests/test_calc.py": (
            "from calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"),
    })


def _make_fragile_repo(tmp_path: Path) -> Path:
    """唯一的变异候选（`+` → `-`）会同时弄红三个用例。"""
    return _init_repo(tmp_path / "fragile", {
        "pytest.ini": _PYTEST_INI,
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": (
            "from calc import add\n\n\n"
            "def test_one():\n"
            "    assert add(1, 1) == 2\n\n\n"
            "def test_two():\n"
            "    assert add(2, 3) == 5\n\n\n"
            "def test_three():\n"
            "    assert add(4, 5) == 9\n"),
    })


def _make_misnamed_repo(tmp_path: Path) -> Path:
    """源文件 gauge.py 由 tests/test_calc.py 覆盖 —— 词干对不上。"""
    return _init_repo(tmp_path / "misnamed", {
        "pytest.ini": _PYTEST_INI,
        "gauge.py": "def double(x):\n    return x * 2\n",
        "tests/test_calc.py": (
            "from gauge import double\n\n\n"
            "def test_double():\n"
            "    assert double(3) == 6\n"),
    })


async def test_generated_task_is_actually_red(tmp_path):
    """产出的任务必须**真的红** —— 断言「生成了 N 条记录」证明不了任何事。"""
    repo = _make_green_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=2,
                               workdir=tmp_path / "w")
    assert len(tasks) == 2, f"应产出 2 个任务，实际 {len(tasks)}"
    for i, t in enumerate(tasks):
        assert t.origin == "mutated" and t.mutation_diff
        assert t.gold_files == ["calc.py"]
        assert t.commit == t.base_commit and t.test_files == []
        # task_id 会被 runner._safe_id 洗成分支名与目录名
        assert t.task_id.startswith("green@mut::calc.py:")
        assert "\n" not in t.task_id and '"' not in t.task_id
        dest = tmp_path / "check" / f"t{i}"
        prepare_task_repo(t, dest)
        fs = await run_full_suite(dest, PytestAdapter(), require_report=True)
        assert t.target_test in fs.ids, \
            f"任务不红：{t.target_test} 不在 {sorted(fs.ids)}"
        # 单点缺陷：变异只该弄红这一个用例，其余照常跑过
        assert fs.ids == {t.target_test}, f"红了不止一个：{sorted(fs.ids)}"


async def test_refuses_a_repo_that_is_already_red(tmp_path):
    """本来就红的仓库上做变异，分不清红是变异造成的还是本来就有的。"""
    repo = _make_red_repo(tmp_path)
    with pytest.raises(RuntimeError, match="不是全绿"):
        await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1,
                           workdir=tmp_path / "w")


async def test_drops_mutations_that_break_too_much(tmp_path):
    """把套件炸掉一半的变异不是好任务 —— 它太显眼，且违反单点缺陷前提。"""
    repo = _make_fragile_repo(tmp_path)
    strict = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                                max_new_failures=1, workdir=tmp_path / "w1")
    assert strict == [], "一个变异弄红 3 个用例，max_new_failures=1 时必须丢弃"
    # 另一侧要有区分度：放宽阈值后同一个变异必须被收下，否则「恒返回空」
    # 也能让上面那条通过
    loose = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                               max_new_failures=3, workdir=tmp_path / "w2")
    assert len(loose) == 1
    assert loose[0].gold_files == ["calc.py"]


async def test_smart_scope_skips_files_no_test_file_is_named_after(tmp_path):
    """词干匹配不到就跳过 —— 漏是安全的，产假任务不是。"""
    repo = _make_misnamed_repo(tmp_path)
    smart = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                               workdir=tmp_path / "w1")
    assert smart == [], "gauge.py 没有同名测试文件，smart 范围下不该产出任务"
    # full 范围能捞回来 —— 证明「smart 跳过」是范围策略造成的，不是
    # 这个仓库压根产不出任务
    full = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                              scope="full", workdir=tmp_path / "w2")
    assert [t.gold_files for t in full] == [["gauge.py"]] * len(full)
    assert len(full) >= 1, "full 范围下 gauge.py 的变异必须能被验出来"


async def test_seed_makes_the_selection_reproducible(tmp_path):
    """同一个 seed 两次跑出同一批任务，换 seed 才允许不同。"""
    repo = _make_green_repo(tmp_path)
    a = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1, seed=3,
                           workdir=tmp_path / "w1")
    b = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1, seed=3,
                           workdir=tmp_path / "w2")
    assert [t.task_id for t in a] == [t.task_id for t in b] != []
