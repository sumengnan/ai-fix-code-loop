import subprocess

from aifix.eval.task import Task
from aifix.eval.workspace import materialize, prepare_task_repo


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _task(h) -> Task:
    return Task(task_id="t1", repo=str(h["path"]), commit=h["commit"],
                base_commit=h["base"], test_files=h["test_files"],
                target_test=h["target"], gold_files=h["gold_files"])


def test_source_is_at_base_but_tests_are_from_commit(history_repo, tmp_path):
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert "a - b" in (dest / "calc.py").read_text(encoding="utf-8")
    assert "test_add" in (dest / "tests" / "test_calc.py").read_text(
        encoding="utf-8")


def test_workspace_is_clean(history_repo, tmp_path):
    """必须干净：aifix 的 preflight 会拒绝不干净的仓库。"""
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert _git(dest, "status", "--porcelain").strip() == ""


def test_tests_are_committed_so_worktree_carries_them(history_repo, tmp_path):
    """worktree 从 HEAD 创建 —— 测试不提交的话根本进不去 agent 的工作区。"""
    dest = prepare_task_repo(_task(history_repo), tmp_path / "w")
    tracked = _git(dest, "show", "HEAD:tests/test_calc.py")
    assert "test_add" in tracked


def test_source_repo_untouched(history_repo, tmp_path):
    before = _git(history_repo["path"], "rev-parse", "HEAD").strip()
    prepare_task_repo(_task(history_repo), tmp_path / "w")
    assert _git(history_repo["path"], "rev-parse", "HEAD").strip() == before
    assert _git(history_repo["path"], "status", "--porcelain").strip() == ""


def test_two_workspaces_are_independent(history_repo, tmp_path):
    """并行评测的前提：两个任务工作区互不影响。"""
    a = prepare_task_repo(_task(history_repo), tmp_path / "a")
    b = prepare_task_repo(_task(history_repo), tmp_path / "b")
    (a / "calc.py").write_text("# 改坏 a\n", encoding="utf-8")
    assert "a - b" in (b / "calc.py").read_text(encoding="utf-8")


def test_materialize_is_idempotent_on_unchanged_tests(history_repo, tmp_path):
    """测试文件在 C^ 与 C 完全一致时不该因「没东西可提交」而炸。"""
    h = history_repo
    dest = materialize(str(h["path"]), h["base"], h["base"],
                       h["test_files"], tmp_path / "w")
    assert (dest / "calc.py").is_file()
    assert _git(dest, "status", "--porcelain").strip() == ""
