"""把一个任务还原成一个可以直接交给 aifix 的仓库。

克隆而不是 worktree：worktree 共享同一个 .git，并行跑几十个任务时
分支名、index、gc 都会互相踩 —— 而 aifix 自己还要在任务仓库里再开一个
worktree。`--local` 克隆走硬链接，几乎不额外占盘。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .task import Task


def _git(cwd: Path | None, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{cwd}）：{res.stderr.strip()}")
    return res.stdout


def materialize(repo: str, base_commit: str, commit: str,
                test_files: list[str], dest: Path) -> Path:
    """dest 处得到：源码停在 base_commit，测试来自 commit，且工作区干净。

    干净是硬要求 —— aifix 的 preflight 会拒绝不干净的仓库；而 worktree
    是从 HEAD 创建的，测试不提交的话根本进不到 agent 的工作区。
    """
    dest = Path(dest)
    _git(None, "clone", "--local", "--quiet", "--no-checkout", repo, str(dest))
    _git(dest, "checkout", "--quiet", base_commit)
    if test_files:
        _git(dest, "checkout", commit, "--", *test_files)
    _git(dest, "config", "user.email", "eval@aifix.local")
    _git(dest, "config", "user.name", "aifix-eval")
    if test_files:
        _git(dest, "add", "--", *test_files)
    # 测试文件在 base 与 commit 完全一致时无事可提交，git commit 会以 1 退出
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest)
    if staged.returncode != 0:
        _git(dest, "commit", "--quiet", "-m",
             f"eval: 取 {commit[:8]} 的测试，源码停在 {base_commit[:8]}")
    return dest


def prepare_task_repo(task: Task, dest: Path) -> Path:
    return materialize(task.repo, task.base_commit, task.commit,
                       task.test_files, dest)
