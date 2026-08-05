from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..adapters.base import FailureSet, Verdict
from ..agents.reviewer import SYSTEM_PROMPT as REVIEWER_PROMPT
from ..agents.reviewer import Review
from ..agents.reviewer import build_prompt as build_review_prompt
from ..agents.reviewer import parse_review
from ..runtime.delivery import Worktree
from ..graph import AifixState, trace_of
from ..checks.metamorphic import Metamorphic, diverging_mutations
from ..checks.necessity import Necessity, unnecessary_changes
from ..checks.signals import analyze
from ..checks.verify import compare
from .baseline import adapters_from_state, run_full_suite, run_scoped


_REFIT_NOTE = """你上一轮的补丁被退回了，改动已回滚。

测试确实转绿了，但你新增的判断里用到了那条测试自己的字面量：

{conds}

照着一条用例的具体输入开后门，只能让那一条用例通过。真实调用换个输入、
换个顺序、多一条少一条，缺陷原样还在——而测试再也发现不了它。

请从产品代码的角度重写：先弄清这段逻辑**本该**遵守的规则（数据是怎么产生的、
上下游怎么用它），再按那条规则改。判断条件里不要出现测试里的那些具体值。"""


def _worktree(state: AifixState) -> Worktree:
    """指向已存在的 worktree —— **不进入上下文管理器**。

    worktree 由 cli.run_once 建立并负责移除；这里只借用 commit / rollback /
    file_at_head 这几个纯路径操作。若在此 `with`，退出时会把还在用的
    worktree 删掉。
    """
    return Worktree(Path(state["repo"]), run_id=state["run_id"])


def new_files_of(wt: Worktree, worktree_path: Path,
                 touched: list[str]) -> list[str]:
    """touched 里**这次新建**的那些文件（HEAD 里没有、工作区里有）。

    `git diff` 看不见未跟踪文件，而新建一个源文件是完全合法的修复 —— 同一条
    理由记在 `Worktree.commit` 的 docstring 里。必要性反查要拿它当单独的单位，
    裁判要拿它补全眼前的改动，两处必须用同一份判据。
    """
    return [p for p in touched
            if wt.file_at_head(p) is None and (worktree_path / p).is_file()]


async def review_patch(state: AifixState, diff: str,
                       client: Any = None) -> tuple[Review | None, int, float]:
    """让裁判模型看一眼补丁。返回 (结论, 花掉的 token, 花掉的钱／人民币)。

    无工具、单步、强制 JSON —— 与 `detect_node` 同一个形状，理由也一样：给它
    工具就等于给它一条能烧掉整轮预算的岔路，而它要回答的是一个只需要读一遍
    diff 的问题。

    结论为 None 有两种来路（调用失败 / JSON 不合规），两种都让这一层**整个不
    发声**。退回某个默认判断是在无中生有：退 suspicious 会让一个 JSON 输出不
    合规的模型把每个补丁都标红，退 plausible 则是拿一次失败的调用给补丁背书。

    token 与钱照常回报给调用方累加 —— 这是真花出去的钱，不记账的话
    `budget_usd` 那道闸就漏了一个口子。
    """
    from harness.context.manager import ContextManager
    from harness.llm.openai_compat import OpenAICompatibleClient, json_output
    from harness.loop.agent_loop import AgentLoop
    from harness.reliability.budget import BudgetTracker
    from harness.tools.base import ToolRegistry

    from ..agents.runner import consume

    cfg = state["config"]
    failure = state["_failures"][state["current"]]
    # 路由缺席时**不建客户端**。cli 那道闸（require_reviewer_route）已经在启动
    # 时拦下了这个组合，这里是第二道 —— 绕过 CLI 直接调 run_once 的路径（评测、
    # 测试夹具、别人的脚本）不吃那道闸，而退回 fixer 的路由是这里唯一「顺手」
    # 的选择，也恰恰是最坏的：同一个模型自己验自己。
    if client is None and cfg.reviewer is None:
        return None, 0, 0.0
    loop = AgentLoop(
        client=client or OpenAICompatibleClient(cfg.reviewer),
        registry=ToolRegistry(),
        context=ContextManager(REVIEWER_PROMPT),
        max_steps=1,
        budget=BudgetTracker(max_tokens=cfg.reviewer_max_tokens),
        model_name=cfg.reviewer.model if cfg.reviewer else "",
        price_map=cfg.price_map,
    )
    with json_output():
        outcome = await consume(loop.run(
            build_review_prompt(failure, diff, state.get("diagnosis"))),
            money=cfg.money)
    review = parse_review(outcome.text) if outcome.ok else None
    return review, outcome.tokens, outcome.cost_cny


async def filter_flaky(baseline: FailureSet, current: FailureSet,
                       rerun) -> tuple[set[str], set[str]]:
    """把「新增失败」拆成确认回归与抖动两部分。

    只在出现新失败时触发重跑，且只重跑那几个用例 —— 成本近似为零，
    却能挡掉绝大部分因抖动导致的误回滚（把一个本来正确的补丁滚掉，
    是这个系统最昂贵的错误）。

    rerun：async callable，接收 test_id 集合，返回重跑后的 FailureSet。
    返回 (确认回归, 判为抖动)。
    """
    new = current.ids - baseline.ids
    if not new:
        return set(), set()
    again = await rerun(sorted(new))
    confirmed = new & again.ids
    return confirmed, new - confirmed


async def verify_node(state: AifixState,
                      reviewer_client: Any = None) -> dict[str, Any]:
    """跑全量、过滤抖动、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapters = adapters_from_state(state)
    worktree_path = Path(state["worktree_path"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    # `require_report=True` 两处都不能省，这是判定的取证环节。报告缺失时返回
    # 空集合，而空集合在这里读成「全绿」：全量那份缺失 → 判 BETTER，一个从未
    # 被验证过的补丁进交付分支；复跑那份缺失 → 真被弄红的用例全被划进抖动，
    # 判定回到 BETTER。不是没验证，是把反证据当成了正证据。
    # 代价是这一轮直接抛出去、run 记成 crash —— 环境坏了的话后面每一轮都会坏，
    # 快速失败比匀速烧钱好。
    current = await run_full_suite(worktree_path, adapters,
                                   require_report=True,
                                   timeout=cfg.test_timeout_seconds)

    # 复跑要知道每条 id 归谁，**两份记账并起来**：要复跑的 id 既可能来自
    # baseline（这一跑已变绿，不在 current.owner 里），也可能是这一跑才冒出来的
    # 新失败（baseline 里没有）。只用一份，另一半会抛「记账里没有它」。
    owners = {**(state.get("_owners") or {}), **current.owner}

    async def _rerun(ids: list[str]) -> FailureSet:
        return await run_scoped(worktree_path, adapters, ids,
                                require_report=True, owner=owners,
                                timeout=cfg.scoped_test_timeout_seconds)

    confirmed, flaky = await filter_flaky(baseline, current, _rerun)
    # 抖动的用例从当前结果里剔除，避免它们把判定拖成 WORSE
    effective = FailureSet({k: v for k, v in current.failures.items()
                            if k not in flaky})
    verdict = compare(baseline, effective, target)
    trace = trace_of(state)

    # 一个字节都没改却判 BETTER = 目标用例在 baseline 里本来就是抖的。放着不管
    # 的话报告会宣称修好了一个它没碰过的 bug。
    touched = state.get("touched") or []
    if verdict is Verdict.BETTER and not touched:
        verdict = Verdict.SAME
        trace.fact("baseline_flaky", target)

    # 信号必须在 commit / rollback 之前算：那时补丁还在工作区，旧内容只能
    # 从 HEAD 拿；rollback 之后新内容就没了，commit 之后 HEAD 就是新内容。
    # 同理，diagnosis 在下面几个返回分支里被清成 None，这里的读取发生在清空
    # 之前，拿到的还是本轮的诊断。
    def _now(p: str) -> str | None:
        """工作区当前内容；补丁把文件删掉时返回 None。"""
        f = worktree_path / p
        return f.read_text(encoding="utf-8") if f.is_file() else None

    # 目标测试的源码，供「新增判断用了测试里的字面量」那一类做参照系。
    # 从**工作区**读而不是 HEAD：两者本该逐字相同（改测试文件是被守卫挡死的），
    # 而万一守卫被绕过了，这一列该照着模型眼前那一份来算。
    # 读不到就传 None —— 适配器给不出 `Failure.file`（三个都会给，但 JUnit XML
    # 里那个属性不是必填）、或文件在这一轮被删掉时，这一类不发声而不是报错。
    target_failure = (state.get("_failures") or {}).get(target)
    test_file = getattr(target_failure, "file", None)
    test_source = _now(test_file) if test_file else None

    # suspect_anchored 缺省当 True：只有 detect_node 会写这个键，而它写的是
    # 这一轮的真值。默认 False 会让所有绕过 detect 的路径（图那条、测试夹具）
    # 悄悄关掉这一列信号——把「没人告诉我」读成「诊断不可信」是反的。
    sig = analyze({p: (wt.file_at_head(p), _now(p)) for p in touched},
                  suspect=(state.get("diagnosis") or {}).get("suspect_file"),
                  suspect_anchored=state.get("suspect_anchored", True),
                  test_source=test_source)

    # 贴着测试写的补丁：判 BETTER，但新增的判断里用了目标测试的字面量。
    #
    # 必须排在必要性反查、裁判、commit **之前**：那三层只对要交付的补丁负责，
    # 而这个补丁马上要被回滚重写，跑它们是白花钱（裁判还要花模型调用）。
    #
    # 只在还有 attempt 额度时退回。额度用尽仍然交付并照旧标注 —— 一条永远拦死
    # 的闸会把「唯一能修好的那个补丁」也扔掉，比交付一个被标注过的可疑补丁更糟。
    # 变形复跑与它同一条通路：两者回答的是同一个问题（这补丁只对那一个例子
    # 成立吗），只是判据不同 —— 一个看补丁里有没有测试的字面量，一个直接扰动
    # 测试再跑。都带对照/可证伪，都不是模型的看法。
    #
    # 只在字面量那一条没报时才跑：报了就已经要退回，再花两次重跑买不到新结论。
    meta = Metamorphic()
    if (verdict is Verdict.BETTER and cfg.metamorphic_check
            and not sig.hardcoded_literals and test_file):
        try:
            meta = await diverging_mutations(
                worktree_path, test_file,
                {p: (wt.file_at_head(p), _now(p)) for p in touched},
                target=target, rerun=_rerun,
                max_mutations=cfg.metamorphic_max_mutations)
        except Exception as e:      # noqa: BLE001 —— 这一层坏了不该挡住交付
            trace.fact("metamorphic_failed", str(e))

    refit = ([f"  - {c}" for c in sig.hardcoded_literals]
             + [f"  - 变形之后就不成立了：{d.label}" for d in meta.diverged])
    if (verdict is Verdict.BETTER and cfg.test_fitting_retry and refit
            and state["attempt"] < cfg.max_attempts):
        trace.fact("verdict", verdict.value)
        if sig.hardcoded_literals:
            trace.fact("rejected_test_fitting", sig.hardcoded_literals)
        if meta.diverged:
            trace.fact("rejected_metamorphic", [d.label for d in meta.diverged])
        wt.rollback()
        return {
            "verdict": verdict.value,
            "attempt": state["attempt"] + 1,
            "results": list(state["results"]),
            "diagnosis": None,
            "retry_note": _REFIT_NOTE.format(conds="\n".join(refit)),
            "spent_tokens": state["spent_tokens"],
            "spent_cny": state["spent_cny"],
            "flaky_filtered": sorted(flaky),
            "confirmed_regressions": sorted(confirmed),
            "signals": list(state.get("signals") or []),
        }

    # 必要性反查：逐个 hunk 反向、只重跑目标用例，撤掉之后目标照样绿的报出来。
    # 只给人看、不改判定，但它要**动工作区**，位置有两条硬约束：
    #
    # 1. 必须在 commit 之前。它会短暂撤掉某个 hunk 再写回；放在 commit 之后的
    #    话进程死在中间会留下「分支上有提交 + 工作区是脏的」。
    # 2. 只在判 BETTER 时跑。SAME / WORSE 的补丁马上要回滚，而且那时目标本来
    #    就是红的，反查的判据根本不成立。
    #
    # 任何意外都吞掉：信号不能挡住一个已验证通过的补丁交付。工作区的逐字还原
    # 由 unnecessary_changes 自己保证（连异常路径）。
    # 新增文件单独传：`git diff` 看不见未跟踪文件，而新建源文件是合法的修复。
    nec = Necessity(unnecessary=[], skipped=[])
    if verdict is Verdict.BETTER and cfg.necessity_check:
        try:
            nec = await unnecessary_changes(
                worktree_path, wt.diff(),
                new_files=new_files_of(wt, worktree_path, touched),
                target=target, rerun=_rerun,
                max_units=cfg.necessity_max_units)
        except Exception as e:      # noqa: BLE001 —— 信号不能挡交付
            trace.fact("necessity_check_failed", str(e))

    # 裁判模型：这几层里唯一由 LLM 产出、也是唯一要花钱的信号。门禁与必要性
    # 反查相同，另加一道 `reviewer_check`（默认关）。
    # **它没有否决权**：没有任何分支会因为它的结论去改 verdict（理由见
    # agents/reviewer.py 的模块 docstring）。
    # 新增文件的内容要拼进去，否则它只看得到一半的改动。
    review: Review | None = None
    review_tokens, review_cny = 0, 0.0
    if verdict is Verdict.BETTER and cfg.reviewer_check:
        try:
            review_diff = wt.diff()
            for p in new_files_of(wt, worktree_path, touched):
                review_diff += f"\n--- 新增文件 {p} ---\n{_now(p) or ''}"
            review, review_tokens, review_cny = await review_patch(
                state, review_diff, client=reviewer_client)
        except Exception as e:      # noqa: BLE001 —— 同上，信号不能挡交付
            trace.fact("reviewer_failed", str(e))

    # commit 放在这里而不是下面的 BETTER 分支里：**它的返回值参与判定**，必须
    # 发生在写 verdict / 信号那几条 fact 之前，否则 facts.jsonl 会先写下一个
    # 随后被推翻的 better。
    #
    # 判据是「提交有没有真的产生」，不是用 git diff 提前猜：git diff 看不见
    # 未跟踪文件，而新建源文件是合法修复，按 diff 判会把它误降级成 SAME。
    #
    # 上面那道 touched 守卫仍然保留：它更早、更便宜，且在 commit 之前就能挡
    # 住「一个字节都没碰」。这一道管的是另一件事 —— 碰了，但补丁被自己的反向
    # 补丁抵消了，touched 非空而暂存区为空。两者的 fact 分开记：复盘要区分的
    # 是模型的行为，不是判定的结果。
    # 交付失败必须在这里接住，不能裸穿上去。`git add` 有两种真实的失败形态：
    # 某条路径匹配不到时退 128 且一条都不暂存；新文件命中 .gitignore 时退 1，
    # 而别的路径已经暂存了。
    #
    # 让 RuntimeError 上抛的后果不是「报错」而是**失联**：report_node 执行不到，
    # 用户拿到一段裸 traceback、没有 report.md，而本次 run 前面几个 failure
    # 已经提交进交付分支的修复也没人告诉他。
    #
    # 接住后降级成 SAME 走既有回滚通路：这一个没交付成是事实，别的已交付的提交
    # 留在分支上，报告照常产出并写明出了什么事。
    delivery_error: str | None = None
    if verdict is Verdict.BETTER:
        try:
            committed = wt.commit(f"fix: {target}", paths=touched)
        except RuntimeError as e:
            verdict = Verdict.SAME
            delivery_error = str(e)
            trace.fact("delivery_failed", delivery_error)
        else:
            if not committed:
                verdict = Verdict.SAME
                trace.fact("patch_cancelled_out", target)

    trace.fact("verdict", verdict.value)
    if flaky:
        trace.fact("flaky_filtered", sorted(flaky))
    if confirmed:
        trace.fact("confirmed_regressions", sorted(confirmed))
    if verdict is not Verdict.BETTER:
        trace.fact("rollback", True)

    # 信号只对**真正交付的补丁**负责：SAME / WORSE 的补丁马上被回滚，它从未
    # 存在过。否则「第 1 轮删了公开符号被回滚、第 2 轮干净修好」会被 eval 记成
    # fix_hits=1 且 signals≥1 —— 而那个组合正是 eval/score.py 定义的规格套利
    # 指纹，方向还偏向爱试错的模型。
    #
    # 每类**各写一条**，value 是整个列表：单位是「类」不是「个」，否则「一个
    # 文件删 10 个符号」记 10、「摊到 20 个文件一个没删」记 1，跨模型就不是
    # 同一把尺。key 名沿用单数 —— facts.jsonl 是数据契约，改名会让历史 run
    # 与新代码对不上。
    signals = list(state.get("signals") or [])
    if verdict is Verdict.BETTER:
        # 只在有信号时写 fact：一条恒定出现的空 fact 会让 facts.jsonl 变噪音
        if sig.removed_public_symbols:
            trace.fact("removed_public_symbol", sig.removed_public_symbols)
        if sig.new_module_state:
            trace.fact("new_module_state", sig.new_module_state)
        if sig.files_outside_suspect:
            trace.fact("files_outside_suspect", sig.files_outside_suspect)
        if sig.hardcoded_literals:
            trace.fact("hardcoded_literal", sig.hardcoded_literals)
        # 刻意**不**进 `eval/runner._SIGNAL_KEYS`：那一列的口径是「零模型调用
        # 的静态信号，每个补丁至多 3 条」，而这一条要跑测试、条数无上界，掺进去
        # 跨模型比的就不是同一把尺，历史 run 的 facts 里也没有它。
        if nec.unnecessary:
            trace.fact("unnecessary_hunk", [asdict(f) for f in nec.unnecessary])
        # 这两条记的是**反查自己的覆盖面**，不是补丁的毛病：撤不下来的单位没有
        # 结论，补丁太大时整层没跑 —— 两种情况下「没报出东西」都不等于「干净」。
        if nec.skipped:
            trace.fact("necessity_unit_skipped", nec.skipped)
        if nec.over_cap:
            trace.fact("necessity_over_cap", nec.over_cap)
        # 额度用尽仍然交付的那条路会走到这里。结论不能丢：报告里必须写明
        # 「这个补丁扛不住扰动」，否则人看到的就只是一句「已修复」。
        if meta.diverged:
            trace.fact("metamorphic_diverged", [d.label for d in meta.diverged])
        # 裁判**判 plausible 时一个字都不写**。一条「裁判认为没问题」的 fact 会
        # 被当成背书，而这一层没有资格背书任何东西 —— 它只在看出问题时开口。
        # 同样不进 `_SIGNAL_KEYS`：那一列的口径是零模型调用的静态信号，掺进一
        # 个不可复现的模型判断，跨模型对比就没法做了（用 A 当裁判量 B，和用 B
        # 当裁判量 A，得到的根本不是同一把尺）。
        if review is not None and review.is_suspicious:
            trace.fact("reviewer_suspicious", review.reason)
        if (not sig.is_empty() or nec.unnecessary or nec.skipped
                or nec.over_cap or meta.diverged
                or (review is not None and review.is_suspicious)):
            # 带上 test_id：多 failure 的 run 里，报告只给一份并集的话，人分
            # 不清是哪一次改动删的符号。这个 key 是**追加**不是替换 ——
            # 核心循环对每个 failure 各跑一轮 verify，替换只会剩最后一轮。
            signals.append({"test_id": target, **asdict(sig),
                            "unnecessary_hunks": [asdict(f)
                                                  for f in nec.unnecessary],
                            "necessity_skipped": nec.skipped,
                            "necessity_over_cap": nec.over_cap,
                            "metamorphic_diverged": [d.label
                                                     for d in meta.diverged],
                            "reviewer_note": (review.reason if review is not None
                                              and review.is_suspicious else "")})
    elif not sig.is_empty():
        # 换一个**不被 eval 计数**的 key（见 eval/runner._SIGNAL_KEYS）：被丢
        # 弃的尝试仍有诊断价值（模型试过什么是复盘的素材），但它不该出现在任
        # 何指标里。
        #
        # value 存三类各自的**列表**，与上面交付侧同尺。存个数的话，
        # facts.jsonl 里会并排出现 `removed_public_symbol: ["mul","f0",…]`
        # （1 条 = 1 类）和 `signals_discarded: 11`（11 = 11 个符号），谁拿这
        # 两个数比大小都会得出错的结论；而且名字全丢了 —— 复盘时不知道它删的
        # 是 mul 还是 add，「模型试过什么」正是这条 fact 存在的理由。
        trace.fact("signals_discarded", asdict(sig))

    # 裁判花掉的钱要记账，三条返回分支一条都不能漏 —— 漏了的话
    # `budget_cny` 那道闸就有一个口子：钱真花出去了，闸上看不见。
    # 单独一个 dict 而不是往每条分支里手抄两行，理由就是「一条都不能漏」。
    #
    # `money=cfg.money` 不能省（见 review_patch）：留默认值的话这一层按默认
    # 汇率折算，而 run 的其余部分按配置的汇率 —— 同一个 run 里两套汇率，
    # 账面上看不出来。
    spent = {"spent_tokens": state["spent_tokens"] + review_tokens,
             "spent_cny": state["spent_cny"] + review_cny}

    results = list(state["results"])
    common = {"flaky_filtered": sorted(flaky),
              "confirmed_regressions": sorted(confirmed),
              # 信号只标注，不参与判定 —— 三态判定仍然只看测试结果。
              "signals": signals}

    # 提交已在上面发生（判定要用它的结果）。降级过的 verdict 到这里只剩一条
    # SAME 通路：consecutive_failures 递增、attempt 递增或记终局、rollback，
    # 全部与「本来就没修好」共用同一段代码 —— 并行写两套只会各自漂移。
    if verdict is Verdict.BETTER:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"], "abort_reason": None})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": 0, **spent, **common}

    wt.rollback()
    # 交付失败要盖过 fix_node 记下的守卫原因：模型这一轮做对了（补丁打上去、
    # 测试也转绿了），栽的是交付那一步，报的原因必须是这一步。
    reason = ("交付失败（git add 未能暂存改动）" if delivery_error
              else state.get("abort_reason") or "max_attempts")
    if state["attempt"] >= cfg.max_attempts:
        results.append({"test_id": target, "verdict": verdict.value,
                        "attempts": state["attempt"],
                        "abort_reason": reason})
        return {"verdict": verdict.value, "current": None, "attempt": 0,
                "results": results, "diagnosis": None,
                "consecutive_failures": state.get("consecutive_failures", 0) + 1,
                **spent, **common}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None, **spent, **common}
