import subprocess

from aifix.eval.mine import is_candidate, split_paths

_DIRS = ["tests", "test"]


def test_splits_tests_from_source():
    tests, src = split_paths(["tests/test_calc.py", "calc.py"], _DIRS)
    assert tests == ["tests/test_calc.py"]
    assert src == ["calc.py"]


def test_test_prefixed_file_outside_test_dir_counts_as_test():
    """有的项目把测试和源码放一起 —— 按目录判会漏。"""
    tests, src = split_paths(["pkg/test_util.py", "pkg/util.py"], _DIRS)
    assert tests == ["pkg/test_util.py"]
    assert src == ["pkg/util.py"]


def test_non_python_files_are_dropped():
    tests, src = split_paths(["README.md", "calc.py", "data.json"], _DIRS)
    assert tests == []
    assert src == ["calc.py"]


def test_candidate_needs_both_sides():
    assert is_candidate(["tests/t.py"], ["a.py"]) is True
    assert is_candidate([], ["a.py"]) is False       # 没动测试 → 没有 oracle
    assert is_candidate(["tests/t.py"], []) is False  # 没动源码 → 没有 gold


def test_empty_commit_is_not_a_candidate():
    assert is_candidate(*split_paths([], _DIRS)) is False


import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
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

    real = mine.run_full_suite
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:                      # 第二次 = C 处那一跑
            raise RuntimeError("测试未产出报告 .aifix-report.xml")
        return await real(*a, **kw)

    monkeypatch.setattr(mine, "run_full_suite", flaky)
    seen = []
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m",
                             on_progress=lambda sha, n, error=None:
                                 seen.append((n, error)))
    assert tasks == []
    assert calls["n"] >= 2, "第二次全量测试没被调用，这个测试没测到东西"
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
