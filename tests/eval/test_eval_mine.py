import subprocess
from pathlib import Path

from aifix.adapters.maven_adapter import MavenAdapter
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.eval.mine import is_candidate, split_paths

from aifix.checks.signals import under_dirs

# 判据由目录列表变成谓词（见 ProjectAdapter.is_test_path），
# 这里包的仍是原来那组目录 —— 用例考的东西一个字没改。
_DIRS = lambda p: under_dirs(p, ["tests", "test"])
_PY = (".py",)


def test_splits_tests_from_source():
    tests, src = split_paths(["tests/test_calc.py", "calc.py"], _DIRS, _PY)
    assert tests == ["tests/test_calc.py"]
    assert src == ["calc.py"]


def test_test_prefixed_file_outside_test_dir_counts_as_test():
    """有的项目把测试和源码放一起 —— 按目录判会漏。"""
    tests, src = split_paths(["pkg/test_util.py", "pkg/util.py"], _DIRS, _PY)
    assert tests == ["pkg/test_util.py"]
    assert src == ["pkg/util.py"]


def test_non_python_files_are_dropped():
    tests, src = split_paths(["README.md", "calc.py", "data.json"], _DIRS, _PY)
    assert tests == []
    assert src == ["calc.py"]


def test_candidate_needs_both_sides():
    assert is_candidate(["tests/t.py"], ["a.py"]) is True
    assert is_candidate([], ["a.py"]) is False       # 没动测试 → 没有 oracle
    assert is_candidate(["tests/t.py"], []) is False  # 没动源码 → 没有 gold


def test_empty_commit_is_not_a_candidate():
    assert is_candidate(*split_paths([], _DIRS, _PY)) is False


def test_fixture_files_under_test_dirs_go_with_the_tests():
    """测试目录下的非 .py 夹具必须跟着测试一起嫁接，否则 ground truth 不可达。"""
    tests, src = split_paths(
        ["tests/test_a.py", "tests/data/golden.json", "tests/fixtures/x.sql",
         "src/pkg/mod.py", "README.md", "assets/logo.png"],
        lambda p: under_dirs(p, ["tests", "test"]), _PY)
    assert tests == ["tests/test_a.py", "tests/data/golden.json",
                     "tests/fixtures/x.sql"]
    # 非测试目录的非 .py 不进 gold_files：gold 是 locate_hit 的判定依据，
    # 衡量的是定位**源文件**的能力，塞进数据文件会稀释这个指标
    assert src == ["src/pkg/mod.py"]


def test_test_prefix_outside_test_dirs_only_applies_to_python():
    """`docs/test_plan.md` 不是测试 —— `test_` 是 Python 的命名约定。

    它落进 test_files 会让 is_candidate 把「只改了源码 + 一份文档」的 commit
    误判成候选：克隆一次、跑两轮 scoped，最后必然一无所获。候选在真实仓库里
    本就占提交总数的六成，再掺进这类假候选，挖掘时间是白烧的。
    """
    tests, src = split_paths(["docs/test_plan.md", "src/pkg/mod.py"],
                             lambda p: under_dirs(p, ["tests"]), _PY)
    assert tests == []
    assert src == ["src/pkg/mod.py"]


def test_conftest_is_test_infrastructure_not_ground_truth():
    """根目录 conftest.py 既不在 test_dirs 里也不以 test_ 开头。"""
    tests, src = split_paths(["conftest.py", "src/pkg/mod.py"], lambda p: under_dirs(p, ["tests"]), _PY)
    assert tests == ["conftest.py"]
    assert src == ["src/pkg/mod.py"]


def test_test_dir_is_matched_as_a_path_prefix_not_just_the_first_segment():
    """多段的测试目录必须能判出来。

    起因不是假设：M5 要做的 MavenAdapter，标准布局是 `src/test/java/...`，
    `test_dirs` 会是 `["src/test"]`，而现在的判据是 `parts[0] in test_dirs`
    —— `parts[0]` 是 `src`，判不出来，整个 Java 测试树会被当成源文件塞进
    gold_files。改成路径前缀匹配，对 pytest 的 `["tests", "test"]` 行为完全
    不变（`tests/x.py` 的前缀就是 `tests`）。
    """
    tests, src = split_paths(
        ["src/test/java/demo/CalcTest.java", "src/main/java/demo/Calc.java"],
        lambda p: under_dirs(p, ["src/test"]), _PY)
    assert tests == ["src/test/java/demo/CalcTest.java"]
    # .java 不是 .py，不进 gold_files —— 这一条由 MavenAdapter 自己的
    # 后缀判定接手，不在本函数的职责里
    assert src == []
    # 前缀必须按**分段**比，不能裸 startswith：`testdata/x.py` 不是
    # `test` 目录下的文件
    tests, src = split_paths(["testdata/x.py"], lambda p: under_dirs(p, ["test"]), _PY)
    assert tests == [] and src == ["testdata/x.py"]


def test_source_suffix_comes_from_the_adapter_not_from_this_module():
    """源文件侧的后缀由适配器回答 —— 写死 `.py` 时 Java 仓库一个任务都挖不出。

    链路是：`.java` 不算源文件 → gold_files 为空 → is_candidate 恒 False →
    `aifix mine` 对任何 Maven 仓库产出 0 个任务，且不报一个错，与「这个仓库
    最近没有红转绿的提交」完全无法区分。

    两个方向都断言：只测「Java 后缀下进 src」的话，一个「什么后缀都收」的
    实现照样能过 —— 而那种实现会把 README.md、pom.xml 全塞进 gold_files，
    稀释 locate_hit 这个指标。
    """
    java = "src/main/java/demo/Calc.java"
    tests, src = split_paths([java], lambda p: under_dirs(p, ["src/test"]),
                             MavenAdapter().source_suffixes())
    assert (tests, src) == ([], [java])
    # 反向：同一条路径在 pytest 的后缀集下不是源文件
    tests, src = split_paths([java], lambda p: under_dirs(p, ["src/test"]),
                             PytestAdapter().source_suffixes())
    assert (tests, src) == ([], [])


def test_a_maven_red_to_green_commit_is_a_candidate():
    """整条链路的目的：同时动了 `src/test/java` 与 `src/main/java` 的 commit。

    这是 M4「测试目录按分段前缀匹配」与本次「源文件后缀问适配器要」两处
    改动合起来才成立的结论，单独任何一处都不够。
    """
    paths = ["src/test/java/demo/CalcTest.java",
             "src/main/java/demo/Calc.java", "pom.xml"]
    adapter = MavenAdapter()
    tests, gold = split_paths(paths, adapter.is_test_path,
                              adapter.source_suffixes())
    assert tests == ["src/test/java/demo/CalcTest.java"]
    assert gold == ["src/main/java/demo/Calc.java"]
    # pom.xml 不进 gold_files：gold 衡量的是 Detector 定位**源文件**的能力
    assert is_candidate(tests, gold) is True
    # 反向：同一个 commit 在 pytest 的后缀集下不是候选 —— 这正是修复前
    # 每一个 Maven 提交的下场
    py_side = split_paths(paths, adapter.is_test_path,
                          PytestAdapter().source_suffixes())
    assert is_candidate(*py_side) is False


import pytest

from aifix.eval.mine import mine_tasks, verify_commit


async def test_verify_finds_the_red_to_green_test(history_repo, tmp_path):
    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")
    assert got == [h["target"]]


async def test_verify_rejects_when_nothing_turns_red(history_repo, tmp_path):
    """拿 C 自己当 base：源码已经是好的，测试不会红 —— 不是任务。"""
    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["commit"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")
    assert got == []


async def test_mine_produces_one_task_per_target(history_repo, tmp_path):
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")
    assert len(tasks) == 1
    t = tasks[0]
    assert t.target_test == history_repo["target"]
    assert t.gold_files == ["calc.py"]
    assert t.base_commit == history_repo["base"]
    assert t.commit == history_repo["commit"]
    assert t.test_files == ["tests/test_calc.py"]
    assert history_repo["commit"][:8] in t.task_id


async def test_mine_respects_max_tasks(history_repo, tmp_path):
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, max_tasks=0, workdir=tmp_path / "m")
    assert tasks == []


async def test_mine_skips_root_commit(history_repo, tmp_path):
    """根提交没有父提交，不能构成任务 —— 且不该抛异常。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=99, workdir=tmp_path / "m")
    assert all(t.base_commit for t in tasks)


async def test_no_tasks_when_the_green_run_produces_no_report(
        history_repo, tmp_path, monkeypatch):
    """C 处那一跑没写出报告时，绝不能吐出任务。

    900 秒超时、进程被杀、沙箱执行失败都会让报告缺失；此前
    `green.ids` 会是空集，`red.ids - green.ids` 于是把 base 处所有红的
    用例全部当成「红转绿」——凭空捏造一整批任务。
    """
    import aifix.eval.mine as mine

    real = mine.run_scoped
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:                      # 第二次 scoped = C 处那一跑
            raise RuntimeError("测试未产出报告 .aifix-recheck.xml")
        return await real(*a, **kw)

    monkeypatch.setattr(mine, "run_scoped", flaky)
    seen = []
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m",
                             on_progress=lambda sha, n, error=None:
                                 seen.append((n, error)))
    assert tasks == []
    assert calls["n"] >= 2, "C 处那一跑没被调用，这个测试没测到东西"
    # 「验证失败被跳过」必须能与「这个 commit 没有可用用例」区分开
    assert any(err is not None for _, err in seen)


async def test_verify_drops_a_test_that_disappeared_at_the_commit(
        history_repo, tmp_path):
    """在 C 处被删掉的用例不是「红转绿」，不能当任务。

    C 删掉一个本来就红的旧测试文件时，它同样不会出现在 C 的失败集合里，
    `red - green` 却会把它算成修好了 —— 拿它当任务，任何模型都不可能通过。
    """
    repo = history_repo["path"]
    legacy = repo / "tests" / "test_legacy.py"

    # C^：calc.py 重新变回有 bug，且多出一个本来就红的旧测试文件
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    legacy.write_text("def test_legacy():\n    assert False\n",
                      encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "break calc, add legacy failing test")
    base = _rev(repo, "HEAD")

    # C：修好 calc.py，同时把那个旧测试文件整个删掉
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    legacy.unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "fix: add 应为加法，并删掉过时的旧测试")
    commit = _rev(repo, "HEAD")

    got = await verify_commit(str(repo), commit, base,
                              ["tests/test_calc.py"], PytestAdapter(),
                              tmp_path / "v")
    assert got == ["tests/test_calc.py::test_add"], (
        "在 C 处消失的用例被当成了红转绿")


async def test_verify_rejects_candidates_the_recheck_never_ran(
        history_repo, tmp_path, monkeypatch):
    """run_scoped 整轮中止（pytest 收集阶段崩掉）时不能把候选全放行。

    真实触发场景：候选 id 是 make_test_id 从 junit 报告合成出来的，不是
    pytest 原生 nodeid。pytest 默认不写 <testcase file="..."> 属性，
    make_test_id 对类内测试的回退路径会拼出不存在的路径，导致 run_scoped
    在收集阶段就整轮中止，写出一份 tests="0" 的空报告 —— require_report
    检查不出这种情况（报告文件确实存在），FailureSet.ran 和 .ids 都是空集。

    此前的实现只看 `cand - recheck.ids`：recheck.ids 为空时这个表达式
    退化成 `cand`，即整批候选被误判成「复跑全绿」而放行。用 monkeypatch
    直接模拟这个空 ran/空 ids 的返回值，比真造一个类内测试仓库更直接。
    """
    import aifix.eval.mine as mine
    from aifix.adapters.base import FailureSet

    real = mine.run_scoped
    calls = {"n": 0}

    async def scoped_collect_crash(*a, **kw):
        # 只让阶段 3（复跑候选）崩掉。阶段 1/2 必须走真的实现，否则阶段 1
        # 就拿不到红用例、提前返回 []，这个断言会变成恒真。
        calls["n"] += 1
        if calls["n"] < 3:
            return await real(*a, **kw)
        # 模拟 pytest 收集阶段崩掉：报告存在但一个用例都没跑到。
        return FailureSet(failures={}, ran=frozenset())

    monkeypatch.setattr(mine, "run_scoped", scoped_collect_crash)

    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")
    assert calls["n"] == 3, (
        f"阶段 3 没被调到（实际 {calls['n']} 次 scoped），这个测试没测到东西")
    assert got == [], "复跑没真跑到的候选，不该被当成「复跑通过」全部放行"


async def test_scoped_stage_does_not_run_the_whole_suite(
        history_repo, tmp_path, monkeypatch):
    """阶段 1/2 只跑 test_files —— 全量只在最后确认时跑一次。

    此前每个候选 commit 要跑两次全量（实测本仓库单次 171 秒），而红转绿的
    判定只需要 test_files 这几个文件。省下的时间直接决定挖掘能覆盖多少
    commit。
    """
    import aifix.eval.mine as mine

    real_scoped, real_full = mine.run_scoped, mine.run_full_suite
    scoped_calls: list[list[str]] = []
    full_calls = {"n": 0}

    async def spy_scoped(worktree, adapter, test_ids, **kw):
        scoped_calls.append(list(test_ids))
        return await real_scoped(worktree, adapter, test_ids, **kw)

    async def spy_full(worktree, adapter, **kw):
        full_calls["n"] += 1
        return await real_full(worktree, adapter, **kw)

    monkeypatch.setattr(mine, "run_scoped", spy_scoped)
    monkeypatch.setattr(mine, "run_full_suite", spy_full)

    h = history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              h["test_files"], PytestAdapter(), tmp_path / "v")

    assert got == [h["target"]], "改成 scoped 之后结论必须还是原来那一条"
    assert len(scoped_calls) == 3, (
        f"应是阶段 1/2/3 三次 scoped，实际 {len(scoped_calls)} 次")
    assert full_calls["n"] == 1, (
        f"全量只该在阶段 4 跑一次，实际 {full_calls['n']} 次")
    # 阶段 1/2 的范围恰好是 test_files —— 多一个文件就是白跑，少一个就漏判
    assert scoped_calls[0] == h["test_files"]
    assert scoped_calls[1] == h["test_files"]
    # 阶段 3 收窄到候选本身
    assert scoped_calls[2] == [h["target"]]


async def test_candidate_red_only_under_scope_is_dropped(
        history_repo, tmp_path):
    """scoped 下红、全量下绿的用例必须被阶段 4 排除。

    评测时 run_task 是用**全量** baseline 复现 target_test 的。一个只在
    单跑时红（依赖某个全局状态没被初始化）的用例，到评测时会因为别的测试
    文件先跑并初始化了那个状态而变绿，于是判成 error —— 安全，但白跑一次
    模型。把这份浪费挪到挖掘时一次性付清。

    仓库形状：`appstate.READY` 在 C^ 是 False、C 是 True；
    `tests/test_a_setup.py` 会把它置 True，且按收集顺序排在目标文件之前。
    于是 tests/test_zz.py 里两个用例在阶段 1/2/3 表现完全一样（scoped 下
    红转绿），只有阶段 4 的全量能把它们分开。
    """
    repo = history_repo["path"]

    # C^：calc 退回有 bug，appstate 未就绪，另有一个会把它置就绪的测试文件
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "appstate.py").write_text("READY = False\n", encoding="utf-8")
    (repo / "tests" / "test_a_setup.py").write_text(
        "import appstate\n\n\n"
        "def test_setup_marks_ready():\n"
        "    appstate.READY = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "break calc, add appstate 与置位测试")
    base = _rev(repo, "HEAD")

    # C：修好 calc、appstate 默认就绪，并新增一个测试文件同时覆盖两者
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "appstate.py").write_text("READY = True\n", encoding="utf-8")
    (repo / "tests" / "test_zz.py").write_text(
        "import appstate\nimport calc\n\n\n"
        "def test_needs_ready():\n"
        "    assert appstate.READY\n\n\n"
        "def test_sum_is_addition():\n"
        "    assert calc.add(2, 3) == 5\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "fix: calc 与 appstate")
    commit = _rev(repo, "HEAD")

    got = await verify_commit(str(repo), commit, base, ["tests/test_zz.py"],
                              PytestAdapter(), tmp_path / "v")

    # 真红的那条留下，只在 scoped 下红的那条被丢掉 —— 两个方向都要成立，
    # 否则「全丢了」或「全留了」都能蒙混过去
    assert got == ["tests/test_zz.py::test_sum_is_addition"], (
        "test_needs_ready 只在 scoped 下红，全量下会被 test_a_setup 救绿，"
        "不该进任务集")


async def test_verify_raises_when_the_full_confirmation_run_ran_nothing(
        history_repo, tmp_path, monkeypatch):
    """阶段 4 的全量一个用例都没跑到 —— 必须抛，不能静默返回 []。

    能走到阶段 4 说明阶段 1 已经跑出过失败，所以「一个用例都没跑到」一定是
    异常（整轮中止、报告为空），不是「这个仓库没测试」。此前 `cand &
    red_full.ids` 会安静地退化成空集，on_progress 那里看到的是 n=0 ——
    与「这个 commit 没有可用用例」这个正常结果完全无法区分，一整轮挖掘可能
    颗粒无收却不报一个错。
    """
    import aifix.eval.mine as mine
    from aifix.adapters.base import FailureSet

    async def full_ran_nothing(*a, **kw):
        return FailureSet(failures={}, ran=frozenset())

    monkeypatch.setattr(mine, "run_full_suite", full_ran_nothing)

    h = history_repo
    with pytest.raises(RuntimeError, match="全量"):
        await verify_commit(str(h["path"]), h["commit"], h["base"],
                            h["test_files"], PytestAdapter(), tmp_path / "v")


async def test_mine_reports_the_failed_full_confirmation_as_a_skip(
        history_repo, tmp_path, monkeypatch):
    """阶段 4 抛出的 RuntimeError 要走「验证失败，已跳过」那条路。"""
    import aifix.eval.mine as mine
    from aifix.adapters.base import FailureSet

    async def full_ran_nothing(*a, **kw):
        return FailureSet(failures={}, ran=frozenset())

    monkeypatch.setattr(mine, "run_full_suite", full_ran_nothing)

    seen = []
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m",
                             on_progress=lambda sha, n, error=None:
                                 seen.append((n, error)))
    assert tasks == []
    # n=-1 且 error 非空，而不是「n=0，没事发生」
    assert [(n, err is not None) for n, err in seen] == [(-1, True)]


async def test_verify_keeps_a_file_that_could_not_be_imported_at_base(
        history_repo, tmp_path):
    """新增测试文件 + 它 import 的新模块 —— 这一整类候选靠文件级 id 才留得住。

    嫁接到 C^ 上时被 import 的模块还不存在，pytest 在收集阶段就 ImportError，
    junit 里发出的是一条文件级 <error>（classname=""、name 是点分模块名），
    make_test_id 还原出来的 id 就是文件路径本身，不带 `::`。

    这种 id 在 C 侧不会出现在 green.ran 里（那一侧该文件正常收集，发出的是
    各个用例），所以 `(red - green) & green.ran` 会把整类候选静默丢掉 ——
    实测本仓库 65 个候选 commit 里有 32 个是这个形状。
    """
    repo = history_repo["path"]
    base = _rev(repo, "HEAD")

    (repo / "shapes.py").write_text(
        "def area(w, h):\n    return w * h\n", encoding="utf-8")
    (repo / "tests" / "test_shapes.py").write_text(
        "from shapes import area\n\n\n"
        "def test_area():\n"
        "    assert area(2, 3) == 6\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "feat: shapes.area 及其测试")
    commit = _rev(repo, "HEAD")

    got = await verify_commit(str(repo), commit, base, ["tests/test_shapes.py"],
                              PytestAdapter(), tmp_path / "v")

    assert got == ["tests/test_shapes.py"], (
        "C^ 处导入失败、C 处正常的测试文件必须以文件级 id 留在候选里")


def test_file_went_green_needs_at_least_one_case_that_actually_ran():
    """整个文件的用例全被 skip 掉不是「变绿」。

    ran 不含 skipped（见 FailureSet.ran），所以「没有失败」在这里同时意味着
    「没有跑」。放行它就是造一个 target_test 永远不会真正执行的假任务。
    """
    from aifix.adapters.base import FailureSet
    from aifix.eval.mine import _file_went_green

    all_skipped = FailureSet(failures={}, ran=frozenset())
    assert _file_went_green("tests/test_x.py", all_skipped,
                            PytestAdapter()) is False


def test_file_went_green_is_false_when_some_case_still_fails():
    """文件里还有用例在红 —— 这个文件没有整体变绿。"""
    from aifix.adapters.base import Failure, FailureSet
    from aifix.eval.mine import _file_went_green

    bad = "tests/test_x.py::test_b"
    partial = FailureSet(
        failures={bad: Failure(test_id=bad, classname="tests.test_x",
                               name="test_b", message="", trace="")},
        ran=frozenset({"tests/test_x.py::test_a", bad}))
    assert _file_went_green("tests/test_x.py", partial,
                            PytestAdapter()) is False
    # 另一侧要有区分度：同样的形状去掉那条失败就该是 True，否则这个断言
    # 恒成立也看不出来
    ok = FailureSet(failures={},
                    ran=frozenset({"tests/test_x.py::test_a", bad}))
    assert _file_went_green("tests/test_x.py", ok, PytestAdapter()) is True


async def test_empty_scope_returns_before_materializing(history_repo, tmp_path,
                                                        monkeypatch):
    """没有可跑的 .py 测试时，不该先克隆一次再返回 []。

    「只改了 tests/data/*.json 夹具 + 源码」的 commit 过得了 is_candidate
    （夹具进 test_files），但 scope 是空的 —— materialize 里那次
    `git clone --local` 纯属白跑。
    """
    import aifix.eval.mine as mine

    repo = history_repo["path"]
    base = _rev(repo, "HEAD")
    (repo / "tests" / "data").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "data" / "golden.json").write_text("{}\n",
                                                         encoding="utf-8")
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # 顺手改一行\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "chore: 加一份夹具，顺手动一下源码")
    commit = _rev(repo, "HEAD")

    real = mine.materialize
    calls: list[tuple] = []

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(mine, "materialize", spy)
    got = await verify_commit(str(repo), commit, base,
                              ["tests/data/golden.json"], PytestAdapter(),
                              tmp_path / "v")
    assert got == []
    assert calls == [], "scope 为空时不该 materialize（里面含一次 git clone）"


_JAVA_TEST = "src/test/java/demo/CalcTest.java"


@pytest.fixture
def java_history_repo(tmp_path: Path) -> dict:
    """一个 Maven 布局的 git 仓库，两个提交，**不跑 mvn**。

    专门用来盯 verify_commit 交给 run_scoped 的 scope 长什么样：那一步在
    materialize 之后、任何测试真正跑起来之前，把 run_scoped 换成间谍就够了。
    真跑 mvn 的验收在 tests/test_maven_mining.py。
    """
    repo = tmp_path / "javahist"
    (repo / "src/main/java/demo").mkdir(parents=True)
    (repo / "src/test/java/demo").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (repo / "src/main/java/demo/Calc.java").write_text(
        "package demo;\npublic class Calc { int add(int a, int b)"
        " { return a - b; } }\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "init")
    base = _rev(repo, "HEAD")

    (repo / "src/main/java/demo/Calc.java").write_text(
        "package demo;\npublic class Calc { int add(int a, int b)"
        " { return a + b; } }\n", encoding="utf-8")
    (repo / _JAVA_TEST).write_text(
        "package demo;\nclass CalcTest { void addWorks() {} }\n",
        encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _commit(repo, "fix: add 应为加法")
    return {"path": repo, "base": base, "commit": _rev(repo, "HEAD")}


async def test_scope_is_translated_into_adapter_selectors(
        java_history_repo, tmp_path, monkeypatch):
    """交给 run_scoped 的是**选择器**，不是改动过的测试文件路径。

    两件事一起坏过：scope 写死 `suffix == ".py"`，Maven 任务的 test_files
    全是 `.java` → scope 为空 → 在 materialize 之前就 return []，
    on_progress 看到 n=0，与「这个 commit 没有可用用例」无法区分。而只把
    后缀放宽（或添一个 `.java`）也不成立 —— scope 原样进
    scoped_test_command，surefire 的 `-Dtest=` 不认路径，喂进去不报错，
    只是一个用例都不跑。
    """
    import aifix.eval.mine as mine
    from aifix.adapters.base import FailureSet

    seen: list[list[str]] = []

    async def spy_scoped(worktree, adapter, test_ids, **kw):
        seen.append(list(test_ids))
        # 阶段 1 拿不到红用例就会提前返回 —— 正合适，本条只看 scope
        return FailureSet(failures={}, ran=frozenset())

    monkeypatch.setattr(mine, "run_scoped", spy_scoped)
    h = java_history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              [_JAVA_TEST], MavenAdapter(), tmp_path / "v")
    assert got == []
    assert seen == [["demo.CalcTest"]], seen


def _maven_stages(stages):
    """按调用次序回放一串 FailureSet，供 verify_commit 的四个阶段消费。

    喂的是 surefire 真实产出的 id 形状（见 tests/test_maven_adapter.py 里
    那几条真跑 mvn 的实测），verify_commit 里的判定才是被测对象。
    """
    from aifix.adapters.base import Failure, FailureSet

    def fs(failed, ran):
        return FailureSet(
            failures={i: Failure(test_id=i, classname="", name="",
                                 message="", trace="") for i in failed},
            ran=frozenset(ran))

    return [fs(f, r) for f, r in stages]


async def test_maven_case_ids_are_not_mistaken_for_class_level_ids(
        java_history_repo, tmp_path, monkeypatch):
    """`::` 是 pytest 的语法，拿它判「文件级 id」会把整个 Maven 候选集清空。

    Maven 的 id 形如 `demo.CalcTest#addWorks`，一个 `::` 都没有。按
    `"::" not in i` 判，**每一个** Maven id 都是文件级的，随后
    _file_went_green 拿 startswith("demo.CalcTest#addWorks::") 去匹配，
    永远匹配不到 → 恒 False → 阶段 3 的过滤把整批候选清空 → verify_commit
    返回 []，且不报一个错，与「这个 commit 没有可用用例」无法区分。
    """
    import aifix.eval.mine as mine

    scoped = _maven_stages([
        # 阶段 1：C^ 源码 + C 测试 —— addWorks 红，zeroIsStable 绿
        ({"demo.CalcTest#addWorks"},
         {"demo.CalcTest#addWorks", "demo.CalcTest#zeroIsStable"}),
        # 阶段 2：C 处全绿
        (set(), {"demo.CalcTest#addWorks", "demo.CalcTest#zeroIsStable"}),
        # 阶段 3：复跑候选，绿
        (set(), {"demo.CalcTest#addWorks"}),
    ])
    full = _maven_stages([
        ({"demo.CalcTest#addWorks"},
         {"demo.CalcTest#addWorks", "demo.CalcTest#zeroIsStable"}),
    ])
    calls = {"scoped": 0, "full": 0}

    async def fake_scoped(worktree, adapter, test_ids, **kw):
        calls["scoped"] += 1
        return scoped[calls["scoped"] - 1]

    async def fake_full(worktree, adapter, **kw):
        calls["full"] += 1
        return full[calls["full"] - 1]

    monkeypatch.setattr(mine, "run_scoped", fake_scoped)
    monkeypatch.setattr(mine, "run_full_suite", fake_full)

    h = java_history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              [_JAVA_TEST], MavenAdapter(), tmp_path / "v")
    # 防空转：四个阶段必须都真的走到了，否则「返回了那一条」可能是
    # 提前返回撞上的
    assert calls == {"scoped": 3, "full": 1}, calls
    assert got == ["demo.CalcTest#addWorks"], got


async def test_maven_class_level_id_survives_when_the_class_goes_green(
        java_history_repo, tmp_path, monkeypatch):
    """类级 id（初始化失败）也要能被识别出来 —— 不能只是「一律不是文件级」。

    没有这一条，一个把 is_file_level_id 写成恒 False 的实现照样能过上面
    那条：Maven 侧「测试类在 C^ 初始化失败、在 C 正常」这一整类候选会被
    静默丢掉，正如 pytest 侧曾经丢掉「文件在 C^ 导入失败」那一类。

    形状取自实测：surefire 对类初始化失败只发一条 name 为空的 testcase，
    合成出的 id 是裸类名 `demo.BootTest`（见 tests/test_maven_adapter.py）。
    """
    import aifix.eval.mine as mine

    scoped = _maven_stages([
        # 阶段 1：整个类在 C^ 初始化就炸，只发一条类级 id
        ({"demo.BootTest"}, {"demo.BootTest"}),
        # 阶段 2：C 处类正常，发的是两个方法，类级 id 根本不出现
        (set(), {"demo.BootTest#a", "demo.BootTest#b"}),
        # 阶段 3：拿类级 id 当选择器复跑（-Dtest=demo.BootTest 是合法的）
        (set(), {"demo.BootTest#a", "demo.BootTest#b"}),
    ])
    full = _maven_stages([({"demo.BootTest"}, {"demo.BootTest"})])
    calls = {"scoped": 0, "full": 0}

    async def fake_scoped(worktree, adapter, test_ids, **kw):
        calls["scoped"] += 1
        return scoped[calls["scoped"] - 1]

    async def fake_full(worktree, adapter, **kw):
        calls["full"] += 1
        return full[calls["full"] - 1]

    monkeypatch.setattr(mine, "run_scoped", fake_scoped)
    monkeypatch.setattr(mine, "run_full_suite", fake_full)

    h = java_history_repo
    got = await verify_commit(str(h["path"]), h["commit"], h["base"],
                              [_JAVA_TEST], MavenAdapter(), tmp_path / "v")
    assert calls == {"scoped": 3, "full": 1}, calls
    assert got == ["demo.BootTest"], got


def _commit(repo, msg: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-q", "-m", msg],
                   cwd=repo, check=True)


def _rev(repo, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_changed_paths_excludes_deletions(history_repo):
    """git show --name-only 也会列出本次删掉的路径。

    如果被删的路径恰好是个测试文件，它会被塞进 test_files，随后
    materialize 里 `git checkout <C> -- <已删除路径>` 必然报 pathspec
    不匹配，整个挖掘就在那个 commit 上崩掉。真实仓库里删测试是常事，
    所以 _changed_paths 必须用 --diff-filter=d 把删除排除在外。
    """
    from aifix.eval.mine import _changed_paths

    repo = history_repo["path"]
    subprocess.run(["git", "rm", "-q", "tests/test_calc.py"],
                   cwd=repo, check=True)
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # 加个注释\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-q", "-m",
                    "remove test file, tweak source"], cwd=repo, check=True)
    new_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()

    paths = _changed_paths(str(repo), new_commit)

    assert "tests/test_calc.py" not in paths
    assert "calc.py" in paths
