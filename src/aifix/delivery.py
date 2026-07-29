"""worktree 隔离：agent 的一切改动都发生在这里，主工作区绝不被触碰。"""
from __future__ import annotations

import subprocess
from pathlib import Path


# 交付提交的署名。**显式给，不用环境里那份。**
#
# 不给的话 git 不会报错，它会从主机名推断一个出来 —— 实测（2026-07-29，macOS）
# 得到 `苏梦楠 <sumengnan@MacBook-Pro-5.local>`，GitHub 的 runner 上会是
# `runner@fv-az….(none)` 这一类。两者都是查无此人的地址，而这条提交是要出现在
# PR 上给人看的。
#
# 用 aifix 自己的身份而不是仓库主的，是因为这条提交**确实不是他写的**：它是一
# 个待审的提议，署成他的名字等于替他签了字。
COMMIT_NAME = "aifix"
COMMIT_EMAIL = "aifix@users.noreply.github.com"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True)


def _git_commit(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """带显式署名的 commit。见 COMMIT_NAME 上面那段。"""
    return _git(repo, "-c", f"user.name={COMMIT_NAME}",
                "-c", f"user.email={COMMIT_EMAIL}", "commit", *args)


def ensure_clean(repo: Path) -> None:
    """已跟踪文件不得有未提交的修改。

    只看**已跟踪**文件（`--untracked-files=no`）。worktree 是从 HEAD 创建的，
    主工作区的未跟踪文件（`__pycache__`、`.venv`、编辑器临时文件、构建产物）
    根本进不去 agent 的工作区——为它们中止，会让任何跑过一次测试、又没把
    `__pycache__` 写进 .gitignore 的项目直接用不了这个工具。

    已跟踪文件的修改则必须拦截：baseline 从 HEAD 算出，若工作区另有改动，
    算出来的失败集合和用户眼前看到的对不上。
    """
    res = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if res.returncode != 0:
        raise RuntimeError(f"不是 git 仓库或 git 不可用：{repo}")
    dirty = [ln for ln in res.stdout.splitlines() if ln.strip()]
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
        """丢弃未提交的改动。已 commit 的轮次不受影响。

        先 `reset` 再 `checkout`：暂存了但没提交的内容同样属于「未提交的改
        动」。少这一句时，`git checkout -- .` 是**从索引**往工作区拷 —— 半个
        暂存区会被原样还原回工作区，等于没回滚。这不是理论边界：新文件命中
        .gitignore 时 `git add` 以 1 退出，而同一条命令里别的路径已经暂存了
        （实测），随后的回滚就落在这个状态上。
        """
        _git(self.path, "reset", "-q")
        _git(self.path, "checkout", "--", ".")
        _git(self.path, "clean", "-fd")

    def file_at_head(self, path: str) -> str | None:
        """worktree 里 HEAD 版本的文件内容；HEAD 里没有这个文件时返回 None。

        信号计算要在 commit 之前做 —— 那时改动还在工作区，旧内容只能
        从 HEAD 拿。
        """
        res = _git(self.path, "show", f"HEAD:{path}")
        return res.stdout if res.returncode == 0 else None

    def commit(self, message: str, paths: list[str]) -> bool:
        """只提交 `paths` 里明确列出的文件，返回**是否真的产生了提交**。

        绝不用 `git add -A`：worktree 里跑测试会产生未跟踪产物
        （__pycache__、覆盖率文件、日志……），全扫进去会污染交付分支。
        paths 来自 ApplyPatchTool 的记账 —— 它是 agent 唯一的修改手段，
        知道自己动过哪些文件。

        失败必须抛。`git add -- <匹配不到的路径>` 退出码是 128、什么都不暂存，
        随后 `git commit` 因无内容以 1 退出 —— 两条都被忽略的话，报告照写
        「修复 1/1」并给出 `git merge`，而交付分支上一个提交都没有。不崩溃、
        不报错、测试全绿，只有承诺是假的。

        「本来就没有改动」是**合法**的：paths 里的文件都在，只是内容与 HEAD
        逐字相同（补丁打上去又被反向补丁抵消，而目标用例本来就是抖的）。它
        与上面那种失败的分界不在 commit 的退出码上 —— 两者都是「无内容可提
        交」—— 而在**暂存结果**：add 报错说明路径本身有问题，add 成功却什么
        都没暂存说明文件真的没变。所以按 add 的退出码判失败，按暂存区判要不
        要提交，commit 的非 0 退出只剩真正的意外。

        返回值就是「交付分支上到底多没多一个提交」，判定侧据此决定要不要说
        「已修复」。它必须由 git 来回答：调用方提前用 `git diff` 猜会漏掉新增
        文件（diff 看不见未跟踪文件），而新建一个源文件是合法的修复。
        """
        if not paths:
            return False
        added = _git(self.path, "add", "--", *paths)
        if added.returncode != 0:
            raise RuntimeError(
                f"交付失败：git add 无法暂存 {'、'.join(paths)} —— "
                f"{added.stderr.strip() or added.stdout.strip()}")
        staged = _git(self.path, "diff", "--cached", "--name-only")
        if not staged.stdout.strip():
            return False
        res = _git_commit(self.path, "-q", "-m", message)
        if res.returncode != 0:
            raise RuntimeError(
                f"交付失败：git commit 未能提交 {'、'.join(paths)} —— "
                f"{res.stderr.strip() or res.stdout.strip()}")
        return True
