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
