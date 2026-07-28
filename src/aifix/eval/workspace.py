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
                test_files: list[str], dest: Path,
                mutation_diff: str | None = None) -> Path:
    """dest 处得到：源码停在 base_commit，测试来自 commit，且工作区干净。

    干净是硬要求 —— aifix 的 preflight 会拒绝不干净的仓库；而 worktree
    是从 HEAD 创建的，测试不提交的话根本进不到 agent 的工作区。

    mutation_diff 非空时（C 类人造变异任务），把它打在 base_commit 之上再
    一并提交 —— 变异不落在源仓库里（那会污染用户的仓库），只随任务集的
    jsonl 走，materialize 时才现场施加。
    """
    dest = Path(dest)
    _git(None, "clone", "--local", "--quiet", "--no-checkout", repo, str(dest))
    _git(dest, "checkout", "--quiet", base_commit)
    if test_files:
        _git(dest, "checkout", commit, "--", *test_files)
    if mutation_diff:
        # git apply 失败必须抛异常，不能静默跳过：没打上变异的任务是绿的，
        # 评测时会被误判成「baseline 未复现目标用例」，白花一次克隆和一次
        # 全量测试，而且看起来像任务集有问题而不是补丁有问题。
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=dest, input=mutation_diff, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"变异补丁打不上（{dest}）：{proc.stderr.strip()}")
    _git(dest, "config", "user.email", "eval@aifix.local")
    _git(dest, "config", "user.name", "aifix-eval")
    if mutation_diff:
        # dest 是刚 clone 出来、还没跑过任何测试的干净目录，不存在
        # __pycache__ 之类的构建产物，所以这里可以放心 add --all 覆盖
        # 补丁改动到的（可能不在 test_files 里的）文件。这与
        # delivery.Worktree.commit 明令禁止 add -A 是两码事——那里的
        # worktree 已经跑过测试，工作区里可能混进了测试产生的垃圾文件。
        _git(dest, "add", "--all")
    elif test_files:
        _git(dest, "add", "--", *test_files)
    # 测试文件在 base 与 commit 完全一致时无事可提交，git commit 会以 1 退出
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest)
    if staged.returncode != 0:
        _git(dest, "commit", "--quiet", "-m",
             f"eval: 取 {commit[:8]} 的测试，源码停在 {base_commit[:8]}")
    return dest


def prepare_task_repo(task: Task, dest: Path) -> Path:
    return materialize(task.repo, task.base_commit, task.commit,
                       task.test_files, dest,
                       mutation_diff=task.mutation_diff)
