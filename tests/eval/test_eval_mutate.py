import ast
from pathlib import Path

from aifix.eval.mutate import Mutation, mutations

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_lineno_follows_cpython_line_counting():
    r"""行号必须按 CPython 分词器的行计数走，不能按 str.splitlines。

    `\x0c`（换页符 / Emacs 分页符，CPython 标准库里就有）不是分词器认的行
    结束符，但 splitlines 在它上面断行。行数组一多出一行，`lines[lineno-1]`
    整体错位：落点、Mutation.lineno、description 里的原值三者一起错，而且
    改出来的源码照样 ast.parse 通过 —— 正是自检抓不住的那一类。
    """
    src = "A = 1\n\x0c\nx = 2 + 3\ny = 4 + 5\n"
    # 按 CPython 认的行结束符切，\x0c 不断行
    real_lines = src.split("\n")
    muts = list(mutations(src))
    # 三行各自的候选都要在：少一条就说明切行把候选也吃掉了
    assert sorted(m.lineno for m in muts) == [1, 3, 3, 3, 4, 4, 4], \
        [(m.lineno, m.description) for m in muts]
    for m in muts:
        changed = [i for i, (a, b) in
                   enumerate(zip(real_lines, m.source.split("\n")), start=1)
                   if a != b]
        assert changed == [m.lineno], (m.description, changed, m.lineno)
        # description 里的「原值」也必须真的取自那一行
        old = m.description.split()[1]
        assert old in real_lines[m.lineno - 1], \
            (m.description, real_lines[m.lineno - 1])


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


def _make_unreadable_repo(tmp_path: Path) -> Path:
    """混进两个 read_text 读不出来的 .py —— git ls-files 照样把它们捞出来。"""
    repo = _init_repo(tmp_path / "unreadable", {
        "pytest.ini": _PYTEST_INI,
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": ("from calc import add\n\n\n"
                               "def test_add():\n"
                               "    assert add(2, 3) == 5\n"),
    })
    (repo / "latin1.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nS = 'caf\xe9'\n")   # 非 UTF-8
    (repo / "dangling.py").symlink_to("nowhere.py")      # 断链符号链接
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-q", "-m", "bad"],
                   cwd=repo, check=True)
    return repo


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
        fs = await run_full_suite(dest, [PytestAdapter()], require_report=True)
        assert t.target_test in fs.ids, \
            f"任务不红：{t.target_test} 不在 {sorted(fs.ids)}"
        # 单点缺陷：变异只该弄红这一个用例，其余照常跑过
        assert fs.ids == {t.target_test}, f"红了不止一个：{sorted(fs.ids)}"


async def test_mutation_diff_applies_under_hostile_git_config(tmp_path,
                                                              monkeypatch):
    """diff 的输出格式必须钉死，否则用户的 ~/.gitconfig 能让整份任务集打不上。

    `diff.noprefix=true` 下裸 `git diff` 产出 `--- calc.py`，而 workspace 的
    `git apply` 走默认 -p1，会以 exit 128 拒收；`color.diff=always` 把 ANSI
    转义写进 diff 正文。`git clone --local` 不隔离全局配置，克隆出来的工作树
    照样吃这两条 —— 后果是每条 mutated 任务都要等到评测时才炸。
    """
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[diff]\n\tnoprefix = true\n"
                         "[color]\n\tdiff = always\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    repo = _make_green_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1,
                               workdir=tmp_path / "w")
    assert len(tasks) == 1
    diff = tasks[0].mutation_diff
    assert "\x1b[" not in diff, f"diff 正文里混进了 ANSI 转义：{diff!r}"
    assert diff.startswith("diff --git a/calc.py b/calc.py"), diff
    # 直接把 diff 喂给 git apply —— 这是评测时真正会走的那条路径
    dest = tmp_path / "apply-here"
    subprocess.run(["git", "clone", "--local", "--quiet", str(repo), str(dest)],
                   check=True)
    proc = subprocess.run(["git", "apply", "--check", "-"], cwd=dest,
                          input=diff, capture_output=True, text=True)
    assert proc.returncode == 0, f"变异补丁打不上：{proc.stderr}"


async def test_candidate_failure_is_reported_not_swallowed(tmp_path,
                                                           monkeypatch):
    """候选跑挂必须上报，不能静默 continue。

    被吞掉的里面包括测试超时（`<` → `<=` 正是制造死循环的经典变异）：用户
    只看到「产出 0 个冒烟任务」，分不出是「没变红」还是「每个候选都跑满超时
    被杀」。顺带钉住超时值 —— 死循环变异是预期内的产物，不该等满 300 秒。
    """
    from aifix.eval import mutate as mutate_mod

    seen: list[tuple[str, int, str | None]] = []
    timeouts: list[float | None] = []

    async def boom(tree, adapter, scope_files, timeout=None):
        timeouts.append(timeout)
        raise RuntimeError("测试未产出报告 x.xml：测试进程没能正常跑完")

    monkeypatch.setattr(mutate_mod, "_run", boom)
    repo = _make_green_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                               workdir=tmp_path / "w",
                               on_progress=lambda p, n, e: seen.append((p, n, e)))
    assert tasks == []
    failed = [s for s in seen if s[1] == -1]
    assert len(failed) == 3, f"calc.py 三个候选全跑挂，应逐个上报：{seen}"
    for ident, _, error in failed:
        assert ident.startswith("calc.py:"), seen      # 标识要指到具体候选
        assert "测试未产出报告" in (error or ""), seen
    assert timeouts and all(t is not None and 0 < t < 300 for t in timeouts), \
        f"候选超时不该沿用 run_scoped 的 300 秒默认值：{timeouts}"


async def test_unreadable_source_does_not_abort_the_round(tmp_path):
    """读不出来的源文件只丢它自己 —— 已收进 tasks 的成果不能跟着陪葬。"""
    seen: list[tuple[str, int, str | None]] = []
    repo = _make_unreadable_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                               scope="full", workdir=tmp_path / "w",
                               on_progress=lambda p, n, e: seen.append((p, n, e)))
    assert [t.gold_files for t in tasks] == [["calc.py"]], \
        f"calc.py 的变异必须活下来：{[t.task_id for t in tasks]}"
    assert {p for p, n, _ in seen if n == -1} == {"latin1.py", "dangling.py"}, \
        seen


async def test_refuses_a_repo_that_is_already_red(tmp_path):
    """本来就红的仓库上做变异，分不清红是变异造成的还是本来就有的。

    抛的必须是 UnusableBaseline 而不是裸 RuntimeError：这是「你的仓库不满足
    前提」，不是「变异这段代码崩了」。CLI 要照前者印一句人话、照后者印调用栈，
    类型分不开就只能一律当崩溃处理。
    """
    from aifix.eval.mutate import UnusableBaseline

    repo = _make_red_repo(tmp_path)
    with pytest.raises(UnusableBaseline, match="不是全绿") as exc:
        await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1,
                           workdir=tmp_path / "w")
    # 点名到具体用例，而不是只报个数 —— 用户下一步要去修的就是它们
    assert "test_calc.py::" in str(exc.value), str(exc.value)


async def test_refuses_a_repo_whose_suite_never_ran(tmp_path, monkeypatch):
    """报告在、一个用例都没跑到：这不是全绿，是压根没跑。

    与上一条同类（前提不成立，不是崩溃），所以走同一个异常类型 —— 放行的话
    下面每个变异的 scoped 也跑不到东西，整轮静默产出 0 个任务，与「这些变异
    都没弄红测试」无法区分。
    """
    from aifix.adapters.base import FailureSet
    from aifix.eval import mutate as mut
    from aifix.eval.mutate import UnusableBaseline

    repo = _make_green_repo(tmp_path)

    async def empty_suite(*a, **kw):
        return FailureSet(failures={}, ran=frozenset())

    monkeypatch.setattr(mut, "run_full_suite", empty_suite)
    with pytest.raises(UnusableBaseline, match="一个用例都没跑到"):
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
    # 非空守卫必须写在前面：full == [] 时下面那条全集比对恒真
    assert len(full) >= 1, "full 范围下 gauge.py 的变异必须能被验出来"
    assert [t.gold_files for t in full] == [["gauge.py"]] * len(full)


def _make_collision_repo(tmp_path: Path) -> Path:
    """同一行上两个可变异算子，且它们弄红的是同一个用例。

    `n > 10` 上的 `>` → `>=` 与 `10` → `11` 都只弄红 test_is_big：源文件、
    行号、target_test 三者全同，task_id 里再没有别的东西能把它们分开。
    """
    return _init_repo(tmp_path / "collide", {
        "pytest.ini": _PYTEST_INI,
        "gate.py": "def is_big(n):\n    return n > 10\n",
        "tests/test_gate.py": (
            "from gate import is_big\n\n\n"
            "def test_is_big():\n"
            "    assert is_big(11) is True\n"
            "    assert is_big(10) is False\n"),
    })


def _make_crlf_repo(tmp_path: Path) -> Path:
    """calc.py 用 CRLF 行尾（Windows 上产生的仓库里很常见）。

    `.gitattributes` 里 `* -text` 关掉 git 的行尾转换，保证不管跑测试的人
    `core.autocrlf` 配成什么，工作区里拿到的都是 CRLF。
    """
    repo = _init_repo(tmp_path / "crlf", {
        "pytest.ini": _PYTEST_INI,
        ".gitattributes": "* -text\n",
        "tests/test_calc.py": _GREEN_TEST,
    })
    (repo / "calc.py").write_bytes(_GREEN_SRC.replace("\n", "\r\n").encode())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-q", "-m", "crlf"],
                   cwd=repo, check=True)
    return repo


async def test_task_ids_are_unique(tmp_path):
    """撞 id 的两个任务在评测时会静默退化成一条「评测故障」。

    runner._safe_id 对相同输入给出相同 run_id，两个任务于是克隆进同一个
    目录，第二个 prepare_task_repo 报 destination already exists，被
    run_suite 的 except 吞成评测故障：分母少一个、故障多一条，看起来像
    环境问题，而真正原因是 id 撞车。`--max-tasks N` 也不再是 N 个不同任务。
    """
    repo = _make_collision_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                               workdir=tmp_path / "w")
    ids = [t.task_id for t in tasks]
    # 非空守卫写在前面：tasks 为空时下面那条唯一性断言恒真
    assert len(tasks) == 2, f"两个算子都该弄红 test_is_big：{ids}"
    assert len(set(ids)) == 2, f"两个任务撞了同一个 id：{ids}"
    for t in tasks:
        # id 会被 _safe_id 洗成分支名与目录名
        assert "\n" not in t.task_id and '"' not in t.task_id


async def test_duplicate_ids_blow_up_instead_of_shipping_a_broken_set(
        tmp_path, monkeypatch):
    """id 撞车是 bug，宁可当场炸也不要静默产出撞车的任务集。

    id 里编进什么都挡不住「两个变异其实完全一样」这种情形，所以出口处必须
    有一道自检 —— 少了它，撞车的代价要到评测跑到一半才以「评测故障」的形式
    露头，而那时已经分不清是环境问题还是任务集问题。
    """
    from aifix.eval import mutate as mutate_mod

    real = mutate_mod.mutations

    def twice(source: str):
        # 同一个变异出现两次：description、行号、变异后源码全同
        first = list(real(source))[:1]
        return iter(first * 2)

    monkeypatch.setattr(mutate_mod, "mutations", twice)
    repo = _make_green_repo(tmp_path)
    with pytest.raises(RuntimeError, match="task_id"):
        await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                           workdir=tmp_path / "w")


async def test_crlf_source_keeps_the_diff_single_point(tmp_path):
    """CRLF 源文件的变异 diff 必须还是单点的，不能是整文件重写。

    `read_text` 走 universal newline 把 `\\r\\n` 归一成 `\\n`、`write_text`
    再写回 `\\n`，整份文件的行尾都被改掉 —— 产出的 diff 恰好是模块 docstring
    要避免的那个形状：既不像真实 bug，也会当场撞上巨型 diff 守卫。
    """
    repo = _make_crlf_repo(tmp_path)
    tasks = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1,
                               workdir=tmp_path / "w")
    assert len(tasks) == 1
    lines = tasks[0].mutation_diff.splitlines()
    minus = [ln for ln in lines if ln.startswith("-") and not ln.startswith("---")]
    plus = [ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
    assert (len(minus), len(plus)) == (1, 1), tasks[0].mutation_diff
    # 补丁要真的打得上 —— 行尾错一个字节，git apply 就找不到上下文
    dest = tmp_path / "apply-here"
    subprocess.run(["git", "clone", "--local", "--quiet", str(repo), str(dest)],
                   check=True)
    proc = subprocess.run(["git", "apply", "--check", "-"], cwd=dest,
                          input=tasks[0].mutation_diff, capture_output=True,
                          text=True)
    assert proc.returncode == 0, f"变异补丁打不上：{proc.stderr}"


async def test_seed_makes_the_selection_reproducible(tmp_path):
    """同一个 seed 两次跑出同一批任务，换 seed 才允许不同。"""
    repo = _make_green_repo(tmp_path)
    a = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1, seed=3,
                           workdir=tmp_path / "w1")
    b = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1, seed=3,
                           workdir=tmp_path / "w2")
    assert [t.task_id for t in a] == [t.task_id for t in b] != []
    # 另一侧要有区分度：seed 不参与选点的话（比如候选顺序被写死），上面那条
    # 照样通过。calc.py 三个候选里 seed=3 取到 `>` → `>=`，seed=0 取到 `+` → `-`
    c = await mutate_tasks(str(repo), PytestAdapter(), max_tasks=1, seed=0,
                           workdir=tmp_path / "w3")
    assert [t.task_id for t in c] != [t.task_id for t in a]


async def test_duplicate_ids_carry_the_verified_tasks_out(tmp_path, monkeypatch):
    """撞车仍然是错误，但已经验证出来的任务不能跟着一起丢掉。

    验证一个候选要真跑一遍测试；一轮变异跑掉半小时是常事。裸 RuntimeError
    穿出去的话 `_cmd_mutate` 的 write_jsonl 根本不会执行 —— 半小时的成果
    一个任务都不落盘，屏幕上只有一段 Python 调用栈。
    """
    from aifix.eval import mutate as mutate_mod
    from aifix.eval.mutate import DuplicateTaskIds

    real = mutate_mod.mutations

    def twice(source: str):
        first = list(real(source))[:1]
        return iter(first * 2)

    monkeypatch.setattr(mutate_mod, "mutations", twice)
    repo = _make_green_repo(tmp_path)
    with pytest.raises(DuplicateTaskIds) as exc:
        await mutate_tasks(str(repo), PytestAdapter(), max_tasks=5,
                           workdir=tmp_path / "w")
    assert isinstance(exc.value, RuntimeError), "调用方按 RuntimeError 接也不能漏"
    assert exc.value.tasks, "已经验证出来的任务必须跟着异常出来"
    assert exc.value.duplicates
    # 报错正文里要点名是哪几个 id，否则人不知道该去看哪一处变异
    assert exc.value.duplicates[0] in str(exc.value)
