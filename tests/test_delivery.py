import subprocess

import pytest

from aifix.delivery import Worktree, ensure_clean


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def test_ensure_clean_passes_on_clean_repo(buggy_repo):
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_ensure_clean_raises_on_dirty_repo(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="工作区不干净"):
        ensure_clean(buggy_repo)


def test_ensure_clean_ignores_aifix_dir(buggy_repo):
    """.aifix/ 是我们自己的产物目录，不能让它把第二次运行卡住。"""
    (buggy_repo / ".aifix" / "runs" / "old").mkdir(parents=True)
    (buggy_repo / ".aifix" / "runs" / "old" / "x.txt").write_text("x", encoding="utf-8")
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_worktree_created_on_new_branch(buggy_repo):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.path.is_dir()
        assert (wt.path / "calc.py").is_file()
        assert wt.branch == "aifix/abc123"
        branches = _git(buggy_repo, "branch", "--list", "aifix/abc123")
        assert "aifix/abc123" in branches


def test_main_worktree_untouched(buggy_repo, fixed_source):
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original


def test_rollback_discards_changes(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.rollback()
        assert "a - b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_commit_keeps_changes_and_rollback_after_is_noop(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.commit("fix: test_add")
        wt.rollback()
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_has_changes_reflects_working_tree(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.has_changes() is False
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        assert wt.has_changes() is True


def test_ensure_clean_ignores_untracked_files(buggy_repo):
    """未跟踪文件不该阻塞：worktree 从 HEAD 创建，它们根本进不去 agent 的工作区。

    真实运行中 __pycache__ 让整个工具直接不可用 —— 任何跑过一次测试
    且未把它加进 .gitignore 的项目都会被卡死。
    """
    (buggy_repo / "src").mkdir(exist_ok=True)
    (buggy_repo / "src" / "__pycache__").mkdir(parents=True)
    (buggy_repo / "src" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (buggy_repo / "scratch.md").write_text("随手笔记", encoding="utf-8")
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_ensure_clean_still_blocks_tracked_modifications(buggy_repo):
    """已跟踪文件被改动仍须拦截：baseline 会和用户眼前的状态对不上。"""
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    (buggy_repo / "untracked.txt").write_text("noise", encoding="utf-8")
    with pytest.raises(RuntimeError, match="工作区不干净"):
        ensure_clean(buggy_repo)
