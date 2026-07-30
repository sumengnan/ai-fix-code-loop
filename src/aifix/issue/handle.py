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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import AifixConfig
from ..graph import COLLECTION_ABORT_KIND, MODEL_ABORT_KIND
from ..delivery import COMMIT_EMAIL, COMMIT_NAME
from ..traces import TRACES_BRANCH
from ..nodes.report import count_fixed
from ..reproduce import (KIND_MISSING_INFO, KIND_NO_CONVERGENCE,
                         KIND_UNPARSEABLE)
from .event import authorize
from .github import GitHubClient


@dataclass
class HandleResult:
    exit_code: int
    path: str                    # ignored / refused / no_repro / delivered / crashed
    pr_url: str | None = None


# 「这次没跑成」的三种中止。口径必须与 `aifix run` 的退出码一致（见
# cli._cmd_run）：预算耗尽（usd / tokens / wall）相反 —— 那是**正常收场**，
# 活干到钱花完为止，结论仍然可信，所以退 0。
_ENV_ABORTS = frozenset({"crash", COLLECTION_ABORT_KIND, MODEL_ABORT_KIND})


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{res.returncode}）：{res.stderr.strip()}")
    return res.stdout


# 三种「没写出复现」的标题。**分开写不是措辞洁癖**：它们的下一步动作完全
# 不同，而人只会读第一行。恒定的「没能写出复现测试」读起来像「你的 issue 不
# 够清楚」，于是运维侧的问题被交给了报 issue 的人 —— 他改多少遍都没用。
_REPRO_HEADLINES = {
    KIND_MISSING_INFO: "**issue 信息不足，写不出复现测试。**",
    KIND_NO_CONVERGENCE: "**模型没能在预算内收敛，这一轮没有产出。**",
    KIND_UNPARSEABLE: "**模型的输出解析不出复现测试。**",
}


def _repro_failure_comment(out: Any) -> str:
    head = _REPRO_HEADLINES.get(
        getattr(out, "kind", ""), "**没能写出复现测试。**")
    return f"{head}\n\n{out.reason}"


def _trace_reproduce(repo: Path, run_id: str, out: Any) -> None:
    """把复现这一步的事件与结论落进 `.aifix/runs/<run_id>/`。

    失败不能影响主流程：这是诊断数据，不是产出。磁盘满、路径没权限都不该让
    一次本来能交付的 run 变成失败。
    """
    from ..trace import RunTrace
    try:
        t = RunTrace(Path(repo) / ".aifix" / "runs" / run_id, run_id=run_id)
        try:
            t.fact("reproduce_kind", getattr(out, "kind", "") or "unknown")
            t.fact("reproduce_tokens", int(getattr(out, "tokens", 0) or 0))
            if getattr(out, "events", None):
                t.record_events(out.events)
        finally:
            t.close()
    except Exception:                           # noqa: BLE001 —— 见上
        pass


def _pr_body(state: dict[str, Any], target: str, issue_number: int,
             repro_tokens: int = 0, repro_usd: float = 0.0) -> str:
    """PR 正文 = 报告 + 必要的背景。

    baseline 里有别的红时**必须出声**：那多半是 runner 的环境漂移，而它会
    污染「这个补丁没弄坏别的」这个判断 —— 审 PR 的人有权知道这次的对照组
    本身就是脏的。不出声的话，一次在坏环境上得出的「没有回归」看起来和一次
    干净的完全一样。
    """
    parts = [f"关联 issue：#{issue_number}", "", state.get("report_md") or ""]

    if repro_tokens:
        # 报告里的成本只统计 run_once 那一段。复现这一步在它之外发起调用，
        # 不单独写出来的话，这笔钱在**任何一份产物里都不存在**。
        cost = "未知" if repro_usd <= 0 else f"${repro_usd:.4f}"
        parts += ["", f"（另：写复现测试花了 {repro_tokens:,} tokens、{cost}，"
                      f"已从上面那次 run 的额度里扣除。）"]

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
    # 从这里开始计时，一直算到 run_once 之前 —— 红检跑的是真测试，耗时不是
    # 可以忽略的量，只掐模型调用那一段等于漏掉一大半。
    t0 = time.monotonic()
    run_id = uuid.uuid4().hex[:8]
    out = await reproduce_fn(repo, adapter, config, ev.title, ev.body)

    # **复现这一步也要落 trace**，哪怕后面根本走不到 run_once。
    #
    # 第一次真跑（2026-07-30，issue #1）时它整段没有 trace —— RunTrace 建在
    # run_once 里，而这条通路走不到那儿，artifact 是空的。于是「模型这 25 步
    # 在读什么」这个唯一有诊断价值的问题，一个字都答不出来。失败时恰恰最需要它。
    #
    # 用**同一个 run_id**：RunTrace 以追加模式开文件，随后 run_once 建的那个
    # 会往同一份 events.jsonl 里继续写，一次 run 的证据留在一个目录里。
    _trace_reproduce(repo, run_id, out)

    r = out.reproduction
    if r is None or not r.can_reproduce:
        gh.comment(ev.number, _repro_failure_comment(out))
        return HandleResult(0, "no_repro")

    written = write_reproduction(repo, r)
    ok, why = await red_check_fn(repo, adapter, r.target_test_id)
    if not ok:
        # 收走它。留着的后果不是「多个文件」：这是一条**红着的**测试，下一次
        # run 的 baseline 会把它算进失败集，于是模型被派去修一个上一次已经判
        # 定为无效的复现。runner 上无所谓（机器就没了），本地和自建 runner 上
        # 是实打实的。
        written.unlink(missing_ok=True)
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
    # **复现那一步的花销要从后面的额度里扣掉。**
    #
    # 它在 run_once 之外发起调用，三层预算闸一分都管不到 —— 不扣的话，设
    # AIFIX_BUDGET_USD=0.50 实际可能花掉两倍，而这个项目对预算的措辞是「越线
    # 之后不再发起新的模型调用」，超支上界必须是可推导的。一个精确措辞但从没
    # 验证过的上界实际超支 4 倍，是这个仓库已经犯过一次的错。
    #
    # 夹到 0，不允许负数：负数会让「还剩多少」的比较全部反向，那时闸最该拦住
    # 的一刻恰好完全不拦（与 fix_node 里 `0.0 or None` 那处同类）。
    # 墙钟同理，而且**它是三层里最要紧的一层**：workflow 靠
    # `AIFIX_BUDGET_WALL_SECONDS < timeout-minutes` 保证软闸先于硬杀响。不扣
    # 的话两段各拿一份完整额度，加起来可能越过 Actions 的硬超时 —— 而硬超时是
    # 直接杀进程，run_once 里那个「保证报告先落地」的 except 执行不到，跑了一
    # 个半小时什么都留不下。红检那一步跑的是真测试，耗时不是可以忽略的量。
    run_config = config.model_copy(update={
        "budget_usd": max(0.0, config.budget_usd - out.cost_usd),
        "budget_tokens": max(0, config.budget_tokens - out.tokens),
        "budget_wall_seconds": max(
            0.0, config.budget_wall_seconds - (time.monotonic() - t0))})

    state = await run_fn(repo, run_config, run_id=run_id,
                         only_test=r.target_test_id)

    # run_once 内部已经保证「报告先落地再返回」（见 cli.run_once 的 except），
    # 所以这里不再包一层 try —— 包了只会把它已经处理好的结果再吞一次。
    body = _pr_body(state, r.target_test_id, ev.number,
                    repro_tokens=out.tokens, repro_usd=out.cost_usd)
    fixed = count_fixed(state.get("results") or [])
    crashed = state.get("abort_kind") == "crash"
    code = 1 if state.get("abort_kind") in _ENV_ABORTS else 0

    branch = state.get("branch") or ""
    if not branch:
        # run_once 在建 worktree **之前**就中止了（解释器配错、模型端点不通）。
        # 没有分支可推，也就没有 PR。
        #
        # 拿空分支名去 push 的后果不是「报错」而是失联：异常裸抛出去，没有 PR、
        # 没有状态评论，issue 里最后一条还停在那个 👀，人只能去 Actions 页面读
        # 一段调用栈 —— 而 run_once 已经把报告准备好了，那里面写着到底出了什么事。
        gh.comment(ev.number,
                   f"**这次 run 没能开始。**\n\n{state.get('report_md') or ''}")
        return HandleResult(code, "aborted")

    # 推不上去（没配远端、认证过期、远端拒绝）不能裸抛：与空分支那条是同一个
    # 失联 —— 异常穿出去就没有 PR、没有说明，issue 里最后一条还停在那个 👀。
    # 区别只在这次**确实是个失败**，所以退非 0 而不是照常收场。
    try:
        git(repo, "push", "origin", branch)
    except Exception as e:                      # noqa: BLE001 —— 见上
        gh.comment(ev.number,
                   f"**修复跑完了，但分支推不上去。**\n\n"
                   f"`{type(e).__name__}：{e}`\n\n"
                   f"分支还在这次 run 的本地仓库里（`{branch}`），但 runner "
                   f"结束后它就没了。下面是这次的报告：\n\n"
                   f"{state.get('report_md') or ''}")
        return HandleResult(1, "push_failed")

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
    return HandleResult(code, "crashed" if crashed else "delivered", url)


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
