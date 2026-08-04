import subprocess

import pytest

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


def _mutation_repo(tmp_path):
    """一个只有一个源文件的干净仓库，供人造变异的落地测试使用。"""
    repo = tmp_path / "src"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD").strip()

    # 用一次真实的 git diff 生成补丁：改内容 → diff → 复原工作区，
    # 这样拿到的补丁是 git apply 认得的真实格式，而不是手写拼凑的。
    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    diff = _git(repo, "diff")
    _git(repo, "checkout", "--", "mod.py")
    return repo, head, diff


def test_materialize_applies_the_mutation_and_leaves_a_clean_tree(tmp_path):
    """变异补丁必须被打上并提交 —— preflight 会拒绝不干净的仓库。"""
    repo, head, diff = _mutation_repo(tmp_path)
    dest = tmp_path / "dest"

    materialize(str(repo), head, head, [], dest, mutation_diff=diff)

    # 两条断言都必须有：只看内容会漏掉「改了但没提交」，
    # 只看干净会漏掉「补丁根本没打上」。
    assert (dest / "mod.py").read_text(encoding="utf-8").strip().endswith(
        "return 2")
    out = _git(dest, "status", "--porcelain")
    assert out.strip() == "", f"工作区不干净：{out}"


def test_materialize_raises_when_mutation_diff_does_not_apply(tmp_path):
    """打不上的补丁必须抛异常，不能悄悄跳过留下一个假绿任务。"""
    repo, head, _ = _mutation_repo(tmp_path)
    bogus_diff = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 999\n"
        "+    return 2\n"
    )
    dest = tmp_path / "dest"

    with pytest.raises(RuntimeError):
        materialize(str(repo), head, head, [], dest, mutation_diff=bogus_diff)
