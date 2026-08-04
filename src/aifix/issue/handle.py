"""一条 `/aifix` 评论 → 一个 PR。编排层，零判定权。

三条交付通路（M6 决策 5）：

| 情形 | 产出 |
|---|---|
| 写不出复现（含红检不过） | 只回帖，列出缺什么。不建分支、不开 PR |
| 写出了复现、没修好 | **推分支 + 回帖**（给出接手命令）。不开 PR |
| 修好了 | 开 PR，报告写进正文 |

第二条推分支的理由：一条红着的复现测试**本身就是产出**，人可以直接接手。
丢掉它等于丢掉这次 run 里唯一有价值的东西 —— 它是真花了钱写出来的，而
runner 一结束就没了。

第二条不开 PR 的理由（0.3.1 改的，此前是开一个标题带「未修复」的 PR）：
PR 的语义是「这些改动请你合」，而这条路上没有任何改动值得合 —— 补丁全被
回滚了，分支与 HEAD 的差别只剩那条复现测试。一个永远合不进去的 PR 会堆在
列表里，而 PR 列表是团队里最不该被噪音填满的地方：用一次就学会跳过它。
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import AifixConfig
from ..graph import (COLLECTION_ABORT_KIND, MODEL_ABORT_KIND,
                     PREFLIGHT_ABORT_KIND)
from ..delivery import COMMIT_EMAIL, COMMIT_NAME
from ..traces import TRACES_BRANCH
from ..nodes.report import count_fixed
from ..progress import TerminalProgress
from ..reproduce import (KIND_CALL_FAILED, KIND_COST_CAPPED,
                         KIND_EMPTY_ANSWER, KIND_MISSING_INFO,
                         KIND_NO_CONVERGENCE, KIND_TRUNCATED,
                         KIND_UNPARSEABLE, ReproduceOutcome)
from .. import pending as pending_store
from ..agents.fixer import format_answer
from ..agents.reproducer import Reproduction
from .event import COMMAND, authorize
from .github import GitHubClient


@dataclass
class HandleResult:
    exit_code: int
    # ignored / refused / no_repro / delivered / unfixed / crashed …
    path: str
    pr_url: str | None = None
    # 这次 run 的 id（交付分支是 `aifix/<run_id>`）。**没修好那条路上没有
    # pr_url**，而调用方仍然要能指认这次 run 的产物在哪 —— 靠解析回帖文本去
    # 拿分支名是把一句给人看的话当成接口用。走不到核心循环的那几条路
    # （拒绝、写不出复现）是 None：那时确实没有 run。
    run_id: str | None = None


# 「这次没跑成」的四种中止。口径必须与 `aifix run` 的退出码一致（见
# cli._FAILED_RUN_KINDS）：预算耗尽（cny / tokens / wall）相反 —— 那是**正常
# 收场**，活干到钱花完为止，结论仍然可信，所以退 0。
#
# preflight 是 2026-08-01 的功能巡检补上的：漏掉它时，Actions 上一次「仓库
# 里没有适配器」的 run 会**绿着结束**，而它一个用例都没跑过。
#
# 这里与 cli 各存一份而不是共用同一个常量，是有意的：两条入口的判据**可以**
# 分叉（比如将来 issue 那边想把某一类算成正常收场），共用一个名字会让分叉
# 变成一次无人察觉的连带修改。代价是要靠这两段注释互相指认 —— 而
# tests/test_abort_kind_parity.py 把它们钉在一起。
_ENV_ABORTS = frozenset({"crash", COLLECTION_ABORT_KIND, MODEL_ABORT_KIND,
                         PREFLIGHT_ABORT_KIND})


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
    KIND_CALL_FAILED: "**模型调用本身失败了 —— 不是模型答不好，是这次调用没跑成。**",
    KIND_UNPARSEABLE: "**模型的输出解析不出复现测试。**",
    KIND_EMPTY_ANSWER: "**模型没有吐出任何正文，这一轮没有产出。**",
    KIND_TRUNCATED: "**这次模型调用中途断了，不是模型的问题。**",
    KIND_COST_CAPPED: "**复现这一步的钱不够走完，被自己的预算闸掐断了。**",
}


def _repro_failure_comment(out: Any) -> str:
    head = _REPRO_HEADLINES.get(
        getattr(out, "kind", ""), "**没能写出复现测试。**")
    return f"{head}\n\n{out.reason}"


def _with_supplement(body: str, supplement: str) -> str:
    """把评论里的补充说明接在 issue 正文后面，一起交给复现器。

    issue 的标题和正文常常写不全，而在评论里补一句是人最自然的动作 —— 从前
    这条路是堵死的，唯一的出路是重开一个 issue。

    **那行「补充说明（来自评论）」是给模型的可读性标注，不是安全边界。**
    issue 正文里完全可以写一段假的同名小节。它不需要是边界：两个来源都已经
    被 `_from_comment` 的两道权限判定要求可信了（评论者 + issue 作者）。
    """
    if not supplement:
        return body
    return f"{body}\n\n---\n补充说明（来自评论）：\n{supplement}".strip()


def _say(line: str) -> None:
    """往 stderr 报一句阶段进展。

    这条流水线在 Actions 上跑几十分钟，而它此前**一个字都不输出** —— 卡住时
    没有任何办法判断卡在哪（artifact 要 job 结束才下载得到，那时已经不叫卡住）。
    核心循环内部的心跳由 `TerminalProgress` 负责，这里补的是它之外那几段：
    复现、红检、推分支、开 PR。

    走 stderr 与 progress 同一条流，两者在 Actions 日志里按时间自然交织；
    stdout 留给 `_cmd_issue` 最后那行结论。
    """
    print(line, file=sys.stderr, flush=True)


def _trace_reproduce(repo: Path, run_id: str, out: Any,
                     answer: dict[str, Any] | None = None) -> None:
    """把复现这一步的事件与结论落进 `.aifix/runs/<run_id>/`。

    `answer`：这一轮带着人的答复跑时，把答复原样记下来。**编号形态天然可审计**
    （选的就是模型自己列的第 N 项），自由回答没有这个性质 —— 不记的话，事后
    回答不了「人当时到底说了什么」，而那正是复盘一次改歪了的修复要问的第一句。

    失败不能影响主流程：这是诊断数据，不是产出。磁盘满、路径没权限都不该让
    一次本来能交付的 run 变成失败。
    """
    from ..trace import RunTrace
    try:
        t = RunTrace(Path(repo) / ".aifix" / "runs" / run_id, run_id=run_id)
        try:
            t.fact("reproduce_kind", getattr(out, "kind", "") or "unknown")
            t.fact("reproduce_tokens", int(getattr(out, "tokens", 0) or 0))
            if answer is not None:
                t.fact("answer", answer)
            if getattr(out, "events", None):
                t.record_events(out.events, out.event_times)
        finally:
            t.close()
    except Exception:                           # noqa: BLE001 —— 见上
        pass


def _pr_body(state: dict[str, Any], target: str, issue_number: int,
             repro_tokens: int = 0, repro_cny: float = 0.0) -> str:
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
        cost = "未知" if repro_cny <= 0 else f"¥{repro_cny:.4f}"
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

    decision = authorize(payload, allowed_users=config.allowed_users)
    if not decision.allowed:
        if decision.notify:
            # 有人在等一个回应。静默丢弃会让他以为它已经在跑了 —— 「不报错、
            # 只有承诺是假的」正是本项目栽过十次以上的那种失败。
            gh.comment(int((payload.get("issue") or {}).get("number", 0)),
                       decision.reason)
            return HandleResult(0, "refused")
        return HandleResult(0, "ignored")

    ev = decision.event
    # 回执打在触发的那条评论上。issue 正文触发时没有那条评论（comment_id 为 0），
    # 这一步跳过 —— 打在 0 号评论上是往别人的帖子上加表情。状态评论随后照发，
    # 「命令被听见了」这件事不会因此丢掉，只是晚几十秒。
    if ev.comment_id:
        gh.react(ev.comment_id)

    adapter = detect_adapter(
        repo, python=resolve_test_python(repo, config.test_python),
        configured=config.adapters)
    if adapter is None:
        gh.comment(ev.number, "没有适配器认领这个项目（支持 pytest 与 Maven）。")
        return HandleResult(0, "no_repro")

    t0 = time.monotonic()
    run_id = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------ 分派
    #
    # `/aifix` 后面那段文字（`ev.text`）**是回答还是补充说明，由状态决定**：
    # issue 上挂着待答问题就是回答，否则就是对本次缺陷的补充。词法在 event.py
    # 里做完了，语义在这里 —— 因为分类要读一次 GitHub，而那个函数是纯的。
    #
    # 状态评论只读一次：两种标记（待答问题、上一次的补充）都在同一条评论里。
    status = gh.status_body(ev.number)
    asked = pending_store.decode_marker(status)
    remembered = pending_store.decode_last(status)

    # 这一次要记住的补充说明。**在任何一次 upsert_status 之前定下来** ——
    # 状态评论是整条覆盖的，携带什么必须先算好（见 _write_status）。
    #
    # 回答那条路不改补充：人是在答问题，不是在改需求，上一次那份要原样传下去。
    supplement = remembered if (asked and ev.text) else (ev.text or remembered)

    def _write_status(body: str, *extra: str) -> None:
        """发状态评论，**统一把要携带的隐藏标记附在末尾**。

        `upsert_status` 是整条覆盖的，而这条评论同时是两种状态的持久层。让三个
        调用点各自记得带标记的话，漏掉任何一个都会让补充说明静默消失 —— 之后
        光 `/aifix` 退回去读 issue 正文，表面照常工作，实际重跑了另一件事。
        收进一个地方，漏不掉。
        """
        parts = [body, *(e for e in extra if e)]
        if supplement:
            parts.append(pending_store.encode_last(supplement))
        gh.upsert_status(ev.number, "\n\n".join(parts))

    # ------------------------------------------------------------ 答复
    #
    # 走的是**重新跑一遍**，不是从断点恢复 —— Actions 的 job 一次性，上一次的
    # 容器连同磁盘一起没了。所以复现测试也得跟着答复一起活下来：它被原样存在
    # 状态评论的隐藏标记里（`pending.encode_marker`），这里取回来重新写下去。
    #
    # 不重跑 reproducer：那要再花一次模型调用，而且**它未必写出同一条测试** ——
    # 人回答的是针对上一条测试的问题，换一条就答非所问了。
    answer_text: str | None = None
    answered: dict[str, Any] | None = None
    if asked and ev.text:
        choice_no = ev.choice
        if choice_no is None:
            # **自由回答。** 原文直接拼进提示词，没有第二次模型调用、没有独立的
            # 意图解析步骤 —— `format_answer` 对两种形态是同一个函数。
            #
            # 从前这条路是被禁的，理由写作「开放式回复要再过一次模型去解析
            # 意图」。那描述的是一种本代码库里并不存在的实现；而整个系统的前提
            # 本来就是「读一段自由文本的缺陷报告然后改代码」，issue 正文就是
            # 自由文本。禁掉它说不通。
            #
            # 但 `ask_user` 那边「模型必须给 2-4 个选项」的硬判**保留** —— 那条
            # 约束真正在防的是提问退化成「我卡住了，救命」，与人怎么回答无关。
            reply = ev.text
        else:
            try:
                reply = pending_store.choose(asked, choice_no)
            except ValueError as e:
                # 越界当场拒。放过去的话它会静静地按另一个选项去改代码，
                # 而人以为自己选的是评论里的那一条。
                gh.comment(ev.number, str(e))
                return HandleResult(0, "bad_choice")
        answer_text = format_answer(asked.get("question", ""), reply)
        # 编号形态天然可审计（选的就是模型自己列的第 N 项），自由回答没有这个
        # 性质。把原文和形态都记下来，否则事后回答不了「人到底说了什么」。
        answered = {"reply": reply, "free_text": choice_no is None,
                    "choice": choice_no}
        repro = asked.get("repro") or {}
        if not repro.get("test_code") or not repro.get("target_test_id"):
            # 标记里没带复现测试（旧版本留下的、或者正文被截断了）。这里裸
            # 构造 Reproduction 的话，test_file 是 None，会一路走到
            # write_reproduction 才以 TypeError 炸掉 —— 那时 issue 上没有任何
            # 说明，人只能去 Actions 页面读调用栈。
            gh.comment(ev.number,
                       "这条待答记录里没有复现测试，没法接着上一轮跑。\n"
                       f"  请重新评论 `{COMMAND}` 从头来一次。")
            return HandleResult(0, "no_pending")
        r = Reproduction(can_reproduce=True, **repro)
        out = ReproduceOutcome(r)               # 这一步零花销：没调模型
        # **回显采纳的是哪种解读。** 同一句话在有无待答问题两种状态下含义不同，
        # 不回显的话人无从知道机器把它当成了什么（见 event._COMMAND_RE 那段）。
        _write_status(
            f"收到答复：**{reply}**\n\n带着它重新跑一次（会再花一次 "
            f"baseline 的时间 —— 这是重跑，不是断点恢复）。")
    else:
        # -------------------------------------------------- 补充说明 / 重跑
        if ev.choice is not None:
            # 没有待答问题却打了一个纯数字。当补充说明跑掉是错的：一个光秃秃的
            # 整数不是缺陷描述，而这一轮要花掉整份预算和几十分钟。这个人几乎
            # 肯定是在回答一个已经不在了的问题（答过了、或者上一轮重跑放弃了）。
            gh.comment(ev.number,
                       "这个 issue 上没有待回答的问题。\n"
                       f"  `{COMMAND} <编号>` 只用来回答 aifix 主动提出的问题；"
                       f"  要发起一次修复，直接评论 `{COMMAND}`，"
                       f"或者用 `{COMMAND} <补充说明>` 把缺陷补充清楚。")
            return HandleResult(0, "no_pending")
        if asked:
            # 挂着问题却光打了一个 `/aifix`：按「上一步出问题了，重试」办 ——
            # 重跑，并**放弃**那个提问（不带答复跑，模型多半会再问一次）。
            #
            # 放弃必须明说。默默丢掉一个人正准备回答的问题，是这条路上最容易
            # 让人以为「我答过了」的失败方式。
            _write_status(
                "收到 `" + COMMAND + "`（没带文字）—— **放弃上一轮那个提问**，"
                "重新跑一遍。\n\n要回答它的话，请重新触发一次并把答复写在 `"
                + COMMAND + "` 后面。")
        elif ev.text:
            _write_status(f"收到补充说明：\n\n> {ev.text}\n\n带着它跑一遍。")
        # ------------------------------------------------------ 复现
        # 从这里开始计时，一直算到 run_once 之前 —— 红检跑的是真测试，耗时
        # 不是可以忽略的量，只掐模型调用那一段等于漏掉一大半。
        _say(f"── 读 issue #{ev.number}，让模型写一条复现测试……")
        out = await reproduce_fn(repo, adapter, config, ev.title,
                                 _with_supplement(ev.body, supplement))

    # **复现这一步也要落 trace**，哪怕后面根本走不到 run_once。
    #
    # 第一次真跑（2026-07-30，issue #1）时它整段没有 trace —— RunTrace 建在
    # run_once 里，而这条通路走不到那儿，artifact 是空的。于是「模型这 25 步
    # 在读什么」这个唯一有诊断价值的问题，一个字都答不出来。失败时恰恰最需要它。
    #
    # 用**同一个 run_id**：RunTrace 以追加模式开文件，随后 run_once 建的那个
    # 会往同一份 events.jsonl 里继续写，一次 run 的证据留在一个目录里。
    _trace_reproduce(repo, run_id, out, answered)

    r = out.reproduction
    if r is None or not r.can_reproduce:
        gh.comment(ev.number, _repro_failure_comment(out))
        return HandleResult(0, "no_repro")

    written = write_reproduction(repo, r)
    _say(f"── 复现测试已写下：{r.test_file} —— 跑一遍确认它真的红了")
    ok, why = await red_check_fn(repo, adapter, r.target_test_id,
                                 timeout=config.scoped_test_timeout_seconds)
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
    # AIFIX_BUDGET_CNY 设的上限实际可能花掉两倍，而这个项目对预算的措辞是「越线
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
        "budget_cny": max(0.0, config.budget_cny - out.cost_cny),
        "budget_tokens": max(0, config.budget_tokens - out.tokens),
        "budget_wall_seconds": max(
            0.0, config.budget_wall_seconds - (time.monotonic() - t0))})

    # **进度必须接上。** 不接的话 Actions 日志里是几十分钟的空屏 —— 实测
    # （2026-08-03，issue #9）那一步只有两行：开头的 env 声明，和 28 分半之后
    # 的一行结果。而命令行那条路一直有心跳，只有这条漏了：`run_once` 的
    # progress 默认是 NullProgress，谁不传谁就静默。
    #
    # 卡住的时候，日志是唯一能实时看到的东西 —— artifact 要 job 结束才下载得
    # 到，而那时已经不叫「卡住」了。
    #
    # 非 TTY 下 TerminalProgress 自动退成逐行输出（见 progress._tty），
    # 不会往 Actions 日志里灌一堆 `\r` 残句。
    _say(f"── 开始修复：{r.target_test_id}")
    state = await run_fn(repo, run_config, run_id=run_id,
                         only_test=r.target_test_id, answer=answer_text,
                         invariant=getattr(r, "invariant", "") or None,
                         progress=TerminalProgress())

    # ------------------------------------------------- 停在「等人回答」上
    #
    # 必须排在建 PR **之前**：那一轮的改动已经被 fix_node 回滚了，交付分支上
    # 除了复现测试什么都没有。给它开一个 PR 等于请人去 review 一个空改动，
    # 而真正需要人做的事（回答那个问题）会被埋在 PR 描述里。
    ask = state.get("ask")
    if ask:
        # 复现测试**原样存进标记**：Actions 的下一个 job 是一个干净的
        # checkout，上一次写下去的文件已经不存在了。不带它的话，答复回来时
        # 只能重跑一次 reproducer —— 那不但要再花一次模型调用，而且它未必写出
        # 同一条测试，人回答的却是针对上一条测试的问题。
        marker = pending_store.encode_marker({
            **pending_store.payload(run_id, str(repo), ask),
            "repro": {"test_file": r.test_file, "test_code": r.test_code,
                      "target_test_id": r.target_test_id},
        })
        _write_status("\n".join([
            "## 需要你回答一个问题", "",
            "复现测试已经写好并跑红了，但要继续改下去，得先确认一件事：", "",
            pending_store.render(ask), "",
            f"回复 `{COMMAND} <编号>`（比如 `{COMMAND} 1`）即可继续，"
            f"也可以直接用自己的话答：`{COMMAND} 空的时候应该抛异常`。",
            "答复之后会**重新跑一遍**，不是从断点继续。",
        ]), marker)
        return HandleResult(0, "needs_input", run_id=run_id)

    # run_once 内部已经保证「报告先落地再返回」（见 cli.run_once 的 except），
    # 所以这里不再包一层 try —— 包了只会把它已经处理好的结果再吞一次。
    body = _pr_body(state, r.target_test_id, ev.number,
                    repro_tokens=out.tokens, repro_cny=out.cost_cny)
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
        return HandleResult(code, "aborted", run_id=run_id)

    # 推不上去（没配远端、认证过期、远端拒绝）不能裸抛：与空分支那条是同一个
    # 失联 —— 异常穿出去就没有 PR、没有说明，issue 里最后一条还停在那个 👀。
    # 区别只在这次**确实是个失败**，所以退非 0 而不是照常收场。
    try:
        _say(f"── 推分支 {branch}")
        git(repo, "push", "origin", branch)
    except Exception as e:                      # noqa: BLE001 —— 见上
        gh.comment(ev.number,
                   f"**修复跑完了，但分支推不上去。**\n\n"
                   f"`{type(e).__name__}：{e}`\n\n"
                   f"分支还在这次 run 的本地仓库里（`{branch}`），但 runner "
                   f"结束后它就没了。下面是这次的报告：\n\n"
                   f"{state.get('report_md') or ''}")
        return HandleResult(1, "push_failed", run_id=run_id)

    # ---------------------------------------------------- 没修好：回帖，不开 PR
    # 分支已经推上去了 —— **那一步不能省**：复现测试是这次 run 真花了钱写出来
    # 的产出，runner 结束就没了。「一条红着的复现测试本身就是产出」这句仍然
    # 成立，改的只是它以什么形状交出去。
    #
    # 为什么不开 PR：PR 的语义是「这些改动请你合」，而这条路上没有任何改动值
    # 得合（补丁全被回滚了，分支与 HEAD 的差别只有那条复现测试）。一个永远合
    # 不进去的 PR 会堆在列表里，而 PR 列表是团队里最不该被噪音填满的地方 ——
    # 用一次就学会跳过它。没修好时该说的是「走到哪儿、卡在哪儿、东西在哪个
    # 分支上」，那是一条评论的形状。
    #
    # 仍然退 0：没修好是**正常收场**，不是故障。这与 `_ENV_ABORTS` 那条判据
    # 是同一套口径 —— 环境坏了才退 1。
    if not fixed and not crashed:
        _say("── 未修复，回帖（不开 PR）")
        archived = _archive(publish, repo, run_id)
        # 走 _write_status 而不是裸的 upsert_status：状态评论是整条覆盖的，
        # 而它同时是「上一次的补充说明」的持久层。这条路漏掉标记的话，人补充
        # 过的说明会在这一次静默消失 —— 之后光 `/aifix` 重跑读回的是 issue
        # 正文，表面照常工作，实际重跑的是另一件事。
        _write_status(_unfixed_status(state, branch, body) + archived)
        return HandleResult(code, "unfixed", run_id=run_id)

    title = f"fix: {ev.title} (#{ev.number})"
    try:
        _say("── 开 PR")
        url = gh.create_pr(head=branch, title=title, body=body)
    except Exception as e:                      # noqa: BLE001
        # 分支**已经推上去了**，成果还在 —— 裸抛的话人连它叫什么都不知道。
        #
        # 实测（2026-07-30，issue #2）撞的是仓库设置默认关闭：
        # `permissions: pull-requests: write` 是必要但**不充分**的，还要
        # Settings → Actions → General → Workflow permissions 里那个复选框。
        # 消息必须指到那一格，不是一句「开 PR 失败了」。
        gh.comment(ev.number,
                   f"**修复跑完了，分支也推上去了，但 PR 没开成。**\n\n"
                   f"`{type(e).__name__}：{e}`\n\n"
                   f"分支：`{branch}` —— 东西都在里面，可以直接 checkout 或手工开 PR。\n\n"
                   f"如果报的是 *not permitted to create and approve pull requests*，"
                   f"那是仓库设置：**Settings → Actions → General → Workflow "
                   f"permissions → 勾上「Allow GitHub Actions to create and approve "
                   f"pull requests」**。job 的 `permissions:` 给够了也不行，这一格是"
                   f"另一道闸。\n\n{state.get('report_md') or ''}")
        return HandleResult(1, "pr_failed")

    # trace 落到孤儿分支上 —— runner 是临时的，不推就全没了（见 aifix.traces）。
    #
    # **失败不能影响交付**：补丁已经推上去、PR 已经开了，为了一次归档失败把
    # 整个 job 弄红，等于让人以为修复没成功。出声但不改结果。
    archived = _archive(publish, repo, run_id)

    _write_status(_status(state, fixed, crashed, url) + archived)
    return HandleResult(code, "crashed" if crashed else "delivered", url,
                        run_id=run_id)


def _archive(publish: Callable[..., bool], repo: Path, run_id: str) -> str:
    """把 trace 推到孤儿分支上，返回报告里那一行。

    **失败不能影响交付**：补丁可能已经推上去了，为了一次归档失败把整个 job
    弄红，等于让人以为修复没成功。出声但不改结果。
    """
    try:
        if publish(repo, run_id):
            return f"\n- trace：`{TRACES_BRANCH}` 分支的 `runs/{run_id}/`"
    except Exception as e:                      # noqa: BLE001 —— 见上
        return f"\n- trace 归档失败（不影响本次交付）：{type(e).__name__}：{e}"
    return ""


def _unfixed_status(state: dict[str, Any], branch: str, body: str) -> str:
    """没修好时的回帖。

    必须给出**接手的命令**，不能只说「东西在 xx 分支上」：这条路的读者刚被
    告知「我没做到」，再让他自己去想怎么把那条复现测试取下来，多半的结果是
    这次 run 的产出没有人碰。

    折叠里放的是 `_pr_body` 而不是裸的 `report_md`。差别是两段**只存在于
    PR 正文里**的东西：复现那一步的花销（它在 run_once 之外发起调用，报告的
    成本不含它），以及 baseline 里本来就有别的红时那句告警。这条路上没有 PR
    了 —— 回帖就是唯一的产物，那两段跟着 PR 一起消失的话，它们在**任何地方
    都不存在**（见 `_pr_body` 的 docstring）。
    """
    return (f"### 🤖 aifix\n\n"
            f"复现测试已就位，但**没能修好**。没有开 PR —— 分支上除了那条"
            f"复现测试没有别的改动，一个合不进去的 PR 只会堆在列表里。\n\n"
            f"- 分支：`{branch}`（那条复现测试在里面，跑起来是**红的**）\n"
            f"- 接手：\n"
            f"  ```bash\n"
            f"  git fetch origin {branch} && git checkout {branch}\n"
            f"  ```\n"
            f"- 再试一次：在这条 issue 下回复 `{COMMAND}`\n\n"
            f"<details><summary>报告（判定、尝试次数、成本都在里面）</summary>"
            f"\n\n{body}\n\n</details>")


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
