"""把一次 run 的**结论**推到一条孤儿分支上，让它活过 runner。

GitHub Actions 的 runner 是临时的：job 一结束，`.aifix/runs/` 连同整台机器
一起消失。而 `ingest` / `stats` 那套跨 run 汇总扫的正是那个目录 —— 在 Actions
上它下面永远只有本次这一个 run，跨 run 统计天然失效。

只推 `facts.jsonl` 与 `report.md`，**不推 `events.jsonl`**。这正是
`trace.py` 开头写下的那条区分：事实是结论，事件是原始素材。前者要长期统计
所以要永久留；后者只在出问题时才要，扔进 artifact（90 天）就够，而且它是三
份里唯一体积会失控的（模型 IO 原文）。

用孤儿分支而不是在 main 上加目录：trace 是运行产物，不该进入任何一次 diff、
不该出现在任何一次 review 里，也不该让 `git log -- src/` 被它稀释。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .delivery import COMMIT_EMAIL, COMMIT_NAME

TRACES_BRANCH = "aifix/traces"

# 推上去的两份。顺序无关，但**这份清单就是契约** —— 加一份进去之前先问一句
# 它是不是「结论」，不是的话它属于 artifact。
PUBLISHED = ("facts.jsonl", "report.md")


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                         text=True)
    if check and res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{res.returncode}）：{res.stderr.strip()}")
    return res.stdout


def publish_traces(repo: Path | str, run_id: str,
                   branch: str = TRACES_BRANCH, remote: str = "origin",
                   git: Callable[..., str] = _git) -> bool:
    """把 `<repo>/.aifix/runs/<run_id>` 里的结论推到 `branch`。

    返回是否推了东西。没有 facts 就返回 False —— 没有可统计的结论时建一条空
    提交只是噪音。

    幂等：同一个 run 推两次，第二次无可提交、照样返回 True。Actions 重跑同一
    个 job 是常事，让它把整个 job 弄红是错的。
    """
    repo = Path(repo)
    src = repo / ".aifix" / "runs" / run_id
    files = [f for f in PUBLISHED if (src / f).is_file()]
    if not any(f == "facts.jsonl" for f in files):
        return False

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "traces"
        # 远端已有这条分支就先拉到本地同名分支；没有是正常情况（第一次跑）。
        # check=False：`fetch` 在远端无此分支时以非 0 退出，那不是错误。
        git(repo, "fetch", "--quiet", remote, f"{branch}:{branch}", check=False)
        try:
            git(repo, "worktree", "add", "--quiet", str(wt), branch)
        except RuntimeError:
            # 本地也没有 —— 开一条真正的孤儿分支。
            #
            # `checkout --orphan` **保留当前工作区的内容与索引**，所以紧接着
            # 必须清空：不清的话第一个提交会把整份源码树复制过来，这条永不合
            # 并的分支会随 run 数线性长胖，而它的用途只是存几十行 jsonl。
            git(repo, "worktree", "add", "--quiet", "--detach", str(wt))
            git(wt, "checkout", "--quiet", "--orphan", branch)
            git(wt, "rm", "-rf", "--quiet", "--ignore-unmatch", ".")

        try:
            dest = wt / "runs" / run_id
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(src / f, dest / f)

            git(wt, "add", "--", f"runs/{run_id}")
            staged = git(wt, "diff", "--cached", "--name-only").strip()
            if staged:
                # 署名与交付提交同一个身份，理由见 delivery.COMMIT_NAME：
                # runner 上没配 git 身份时会从主机名推断出一个查无此人的地址。
                git(wt, "-c", f"user.name={COMMIT_NAME}",
                    "-c", f"user.email={COMMIT_EMAIL}",
                    "commit", "--quiet", "-m", f"trace: {run_id}")
                git(wt, "push", "--quiet", remote, f"HEAD:{branch}")
        finally:
            # 不清理的话，下一次 run 会撞上「路径已被占用」而失败，而那个报错
            # 一个字都不会提到 trace 持久化。
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
    return True
