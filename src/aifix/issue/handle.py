"""一条 `/aifix` 评论 → 一个 PR。编排层，零判定权。

三条交付通路（M6 决策 5）：

| 情形 | 产出 |
|---|---|
| 写不出复现（含红检不过） | 只回帖，列出缺什么。不建分支、不开 PR |
| 写出了复现、没修好 | 照样开 PR，标题标明「未修复」 |
| 修好了 | 开 PR，报告写进正文 |

第二条的理由：一条红着的复现测试**本身就是产出**，人可以直接接手。丢掉它
等于丢掉这次 run 里唯一有价值的东西。
"""
from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import AifixConfig
from ..delivery import COMMIT_EMAIL, COMMIT_NAME
from ..traces import TRACES_BRANCH
from ..nodes.report import count_fixed
from .event import authorize
from .github import GitHubClient


@dataclass
class HandleResult:
    exit_code: int
    path: str                    # ignored / refused / no_repro / delivered / crashed
    pr_url: str | None = None


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{res.returncode}）：{res.stderr.strip()}")
    return res.stdout


def _pr_body(state: dict[str, Any], target: str,
             issue_number: int) -> str:
    """PR 正文 = 报告 + 必要的背景。

    baseline 里有别的红时**必须出声**：那多半是 runner 的环境漂移，而它会
    污染「这个补丁没弄坏别的」这个判断 —— 审 PR 的人有权知道这次的对照组
    本身就是脏的。不出声的话，一次在坏环境上得出的「没有回归」看起来和一次
    干净的完全一样。
    """
    parts = [f"关联 issue：#{issue_number}", "", state.get("report_md") or ""]

    others = [i for i in (state.get("baseline_ids") or []) if i != target]
    if others:
        parts += [
            "", "---", "",
            "> **注意：baseline 里本来就有别的失败用例**，它们不是本次要修的：",
            "", *(f"> - `{i}`" for i in others), "",
            "> 这多半是运行环境与本地的差异（runner 的镜像会漂移）。"
            "aifix 没有因此中止——一个抖动的用例不该把整次 run 毁掉——但"
            "「这个补丁没弄坏别的」这个结论在这种 baseline 上要打折扣。",
        ]
    return "\n".join(parts)


async def handle(
    payload: dict[str, Any],
    repo: Path,
    config: AifixConfig,
    gh: GitHubClient,
    *,
    reproduce_fn: Callable[..., Any] | None = None,
    publish: Callable[..., bool] | None = None,
    red_check_fn: Callable[..., Any] | None = None,
    run_fn: Callable[..., Any] | None = None,
    git: Callable[..., str] = _git,
) -> HandleResult:
    """四个 `*_fn` 是测试注入口；产品路径全传 None，走真实实现。

    延迟导入是必须的而不是风格选择：`reproduce` 会把 `nodes.baseline` 连同
    harness 沙箱一起拉进来，而 `authorize` 那条最常走的路（普通评论）根本用
    不到它们 —— 一条闲聊不该付整条依赖树的导入代价。
    """
    from ..cli import run_once
    from ..nodes.baseline import detect_adapter
    from ..reproduce import red_check, reproduce, write_reproduction
    from ..traces import publish_traces
    from ..adapters.pytest_adapter import resolve_test_python

    reproduce_fn = reproduce_fn or reproduce
    publish = publish or publish_traces
    red_check_fn = red_check_fn or red_check
    run_fn = run_fn or run_once

    decision = authorize(payload)
    if not decision.allowed:
        if decision.notify:
            # 有人在等一个回应。静默丢弃会让他以为它已经在跑了 —— 「不报错、
            # 只有承诺是假的」正是本项目栽过十次以上的那种失败。
            gh.comment(int((payload.get("issue") or {}).get("number", 0)),
                       decision.reason)
            return HandleResult(0, "refused")
        return HandleResult(0, "ignored")

    ev = decision.event
    gh.react(ev.comment_id)

    adapter = detect_adapter(
        repo, python=resolve_test_python(repo, config.test_python))
    if adapter is None:
        gh.comment(ev.number, "没有适配器认领这个项目（支持 pytest 与 Maven）。")
        return HandleResult(0, "no_repro")

    # ---------------------------------------------------------- 复现
    out = await reproduce_fn(repo, adapter, config, ev.title, ev.body)
    r = out.reproduction
    if r is None or not r.can_reproduce:
        gh.comment(ev.number, f"**没能写出复现测试。**\n\n{out.reason}")
        return HandleResult(0, "no_repro")

    write_reproduction(repo, r)
    ok, why = await red_check_fn(repo, adapter, r.target_test_id)
    if not ok:
        # 原因要原样带出来：人看到「没有失败」和看到「收集错误」时，下一步
        # 动作完全不同。归并成一句「复现失败」等于把诊断信息扔了。
        gh.comment(ev.number,
                   f"**写出了复现测试，但它不成立。**\n\n{why}\n\n"
                   f"<details><summary>它写的是这个</summary>\n\n"
                   f"```\n{r.test_code}```\n\n</details>")
        return HandleResult(0, "no_repro")

    # ---------------------------------------------------------- 进 HEAD
    # 必须在 run_once 之前 commit：worktree 从 HEAD 建，baseline 才认得出这是
    # 一个失败用例。顺序反了的话队列是空的，run 以「没活干」正常收场、退 0，
    # 而报告会说「你的仓库没问题」。
    git(repo, "add", "--", r.test_file)
    # 署名与交付提交同一个身份（见 delivery.COMMIT_NAME）：这条测试是 aifix
    # 写的，不是仓库主写的。而且不能靠环境里那份 —— runner 上没配身份时 git
    # 会从主机名推断出一个查无此人的地址，直接印在 PR 上。
    git(repo, "-c", f"user.name={COMMIT_NAME}",
        "-c", f"user.email={COMMIT_EMAIL}", "commit", "-q", "-m",
        f"test: 复现 #{ev.number} —— {ev.title}", "--", r.test_file)

    # ---------------------------------------------------------- 核心循环
    run_id = uuid.uuid4().hex[:8]
    state = await run_fn(repo, config, run_id=run_id,
                         only_test=r.target_test_id)

    # run_once 内部已经保证「报告先落地再返回」（见 cli.run_once 的 except），
    # 所以这里不再包一层 try —— 包了只会把它已经处理好的结果再吞一次。
    body = _pr_body(state, r.target_test_id, ev.number)
    fixed = count_fixed(state.get("results") or [])
    crashed = state.get("abort_kind") == "crash"

    branch = state.get("branch") or ""
    git(repo, "push", "origin", branch)
    title = (f"fix: {ev.title} (#{ev.number})" if fixed
             else f"[复现已就位，未修复] {ev.title} (#{ev.number})")
    url = gh.create_pr(head=branch, title=title, body=body)

    # trace 落到孤儿分支上 —— runner 是临时的，不推就全没了（见 aifix.traces）。
    #
    # **失败不能影响交付**：补丁已经推上去、PR 已经开了，为了一次归档失败把
    # 整个 job 弄红，等于让人以为修复没成功。出声但不改结果。
    archived = ""
    try:
        if publish(repo, run_id):
            archived = f"\n- trace：`{TRACES_BRANCH}` 分支的 `runs/{run_id}/`"
    except Exception as e:                      # noqa: BLE001 —— 见上
        archived = f"\n- trace 归档失败（不影响本次交付）：{type(e).__name__}：{e}"

    gh.upsert_status(ev.number,
                     _status(state, fixed, crashed, url) + archived)
    return HandleResult(1 if crashed else 0,
                        "crashed" if crashed else "delivered", url)


def _status(state: dict[str, Any], fixed: int, crashed: bool,
            url: str) -> str:
    head = ("运行**异常中断**，但下面这个 PR 里的东西是真的"
            if crashed else
            ("修好了" if fixed else "复现已就位，**没能修好**"))
    return (f"### 🤖 aifix\n\n{head}。\n\n"
            f"- PR：{url}\n"
            f"- 分支：`{state.get('branch')}`\n\n"
            f"<details><summary>报告</summary>\n\n"
            f"{state.get('report_md') or ''}\n\n</details>")
