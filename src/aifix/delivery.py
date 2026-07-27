"""worktree 隔离：agent 的一切改动都发生在这里，主工作区绝不被触碰。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True)


def ensure_clean(repo: Path) -> None:
    """主工作区必须干净——否则无法区分哪些改动是 agent 造成的。

    `.aifix/` 例外：worktree 和运行产物就落在那里，把它算进"不干净"
    会让第二次运行直接中止。
    """
    res = _git(repo, "status", "--porcelain")
    if res.returncode != 0:
        raise RuntimeError(f"不是 git 仓库或 git 不可用：{repo}")
    dirty = [ln for ln in res.stdout.splitlines()
             if ln.strip() and not ln[3:].lstrip('"').startswith(".aifix/")]
    if dirty:
        raise RuntimeError(
            "工作区不干净，请先提交或 stash：\n" + "\n".join(dirty))


class Worktree:
    """在 .aifix/runs/<run_id>/tree 建立独立分支的 worktree，退出时移除。"""

    def __init__(self, repo: Path, run_id: str) -> None:
        self.repo = Path(repo)
        self.run_id = run_id
        self.branch = f"aifix/{run_id}"
        self.root = self.repo / ".aifix" / "runs" / run_id
        self.path = self.root / "tree"

    def __enter__(self) -> "Worktree":
        self.root.mkdir(parents=True, exist_ok=True)
        res = _git(self.repo, "worktree", "add", "-b", self.branch,
                   str(self.path), "HEAD")
        if res.returncode != 0:
            raise RuntimeError(f"创建 worktree 失败：{res.stderr.strip()}")
        return self

    def __exit__(self, *exc: object) -> None:
        # 只移除 worktree 目录，**保留分支**——分支是交付物
        _git(self.repo, "worktree", "remove", "--force", str(self.path))

    def has_changes(self) -> bool:
        return bool(_git(self.path, "diff", "--stat").stdout.strip())

    def diff(self) -> str:
        return _git(self.path, "diff").stdout

    def rollback(self) -> None:
        """丢弃未提交的改动。已 commit 的轮次不受影响。"""
        _git(self.path, "checkout", "--", ".")
        _git(self.path, "clean", "-fd")

    def commit(self, message: str) -> None:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)
