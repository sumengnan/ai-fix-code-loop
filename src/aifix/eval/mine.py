"""从 git history 挖任务集。

规格 §9 的做法：
    找出让测试从红变绿的 commit C
    任务 = checkout 到 C^，但保留 C 中的测试文件
    期望 = agent 的补丁让该测试转绿且不引入回归
    对照 = C 中的源码改动即标准答案

自带 ground truth，分布真实 —— 不需要人来标注，也不会像人造变异那样
在分布上跑偏。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

from ..adapters.pytest_adapter import PytestAdapter
from ..nodes.baseline import run_full_suite
from .task import Task
from .workspace import materialize


def split_paths(paths: list[str],
                test_dirs: list[str]) -> tuple[list[str], list[str]]:
    """把 commit 改动的路径拆成（测试文件, 源文件）。"""
    tests: list[str] = []
    src: list[str] = []
    for p in paths:
        pp = PurePosixPath(p)
        if pp.suffix != ".py":
            continue
        # 目录判 + 文件名判：有的项目把测试和源码放在一起
        if (pp.parts and pp.parts[0] in test_dirs) or pp.name.startswith("test_"):
            tests.append(p)
        else:
            src.append(p)
    return tests, src


def is_candidate(test_files: list[str], gold_files: list[str]) -> bool:
    """同时动了测试与源码才可能是「红转绿」。

    只动测试 → 没有 gold；只动源码 → 没有判定用的 oracle。
    """
    return bool(test_files) and bool(gold_files)


def _git(repo: str | Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()}")
    return res.stdout


def _changed_paths(repo: str, commit: str) -> list[str]:
    """本次 commit 改动的路径。

    `--diff-filter=d`（小写 d = 排除删除）很关键：被删除的路径同样会被
    `--name-only` 列出来，若它是个测试文件就会进 test_files，随后
    materialize 的 `git checkout <C> -- <已删除路径>` 必然报 pathspec
    不匹配，整个挖掘就在那个 commit 上崩掉。真实仓库里删测试是常事。
    """
    out = _git(repo, "show", "--name-only", "--diff-filter=d",
               "--pretty=format:", commit)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _parent(repo: str, commit: str) -> str | None:
    """根提交没有父提交 —— 返回 None 而不是抛。"""
    try:
        return _git(repo, "rev-parse", f"{commit}^").strip()
    except RuntimeError:
        return None


async def verify_commit(repo: str, commit: str, base_commit: str,
                        test_files: list[str], adapter: PytestAdapter,
                        workdir: Path) -> list[str]:
    """返回「在 C^ 处红、在 C 处绿」的用例；不成立返回 []。

    两次全量测试，很贵 —— 但这是 ground truth 的来源，省不得。
    筛选（split_paths / is_candidate）已经把绝大多数 commit 挡在了外面。
    """
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    materialize(repo, base_commit, commit, test_files, workdir)
    red = await run_full_suite(workdir, adapter)
    if not red.ids:
        return []
    _git(workdir, "checkout", "--force", "--quiet", commit)
    green = await run_full_suite(workdir, adapter)
    return sorted(red.ids - green.ids)


async def mine_tasks(repo: str, adapter: PytestAdapter, limit: int = 50,
                     max_tasks: int = 10, workdir: Path | None = None,
                     on_progress=None) -> list[Task]:
    """扫最近 limit 个提交，产出至多 max_tasks 个任务。"""
    workdir = Path(workdir or Path(repo) / ".aifix" / "mine")
    workdir.mkdir(parents=True, exist_ok=True)
    name = Path(repo).name
    tasks: list[Task] = []

    shas = _git(repo, "log", "--no-merges", "--format=%H",
                f"-n{limit}").split()
    for sha in shas:
        if len(tasks) >= max_tasks:
            break
        base = _parent(repo, sha)
        if base is None:
            continue
        test_files, gold_files = split_paths(
            _changed_paths(repo, sha), adapter.test_dirs())
        if not is_candidate(test_files, gold_files):
            continue
        targets = await verify_commit(repo, sha, base, test_files,
                                      adapter, workdir / sha[:8])
        if on_progress:
            on_progress(sha, len(targets))
        for t in targets:
            if len(tasks) >= max_tasks:
                break
            tasks.append(Task(
                task_id=f"{name}@{sha[:8]}::{t}",
                repo=str(Path(repo).resolve()), commit=sha, base_commit=base,
                test_files=test_files, target_test=t, gold_files=gold_files,
                adapter=adapter.name))
    shutil.rmtree(workdir, ignore_errors=True)
    return tasks
