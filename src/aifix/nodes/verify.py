from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..adapters.base import FailureSet, Verdict
from ..delivery import Worktree
from ..graph import AifixState, trace_of
from ..necessity import unnecessary_changes
from ..signals import analyze
from ..verify import compare
from .baseline import adapters_from_state, run_full_suite, run_scoped


def _worktree(state: AifixState) -> Worktree:
    """指向已存在的 worktree —— **不进入上下文管理器**。

    worktree 由 cli.run_once 建立并负责移除；这里只借用 commit / rollback /
    file_at_head 这几个纯路径操作。若在此 `with`，退出时会把还在用的
    worktree 删掉。
    """
    return Worktree(Path(state["repo"]), run_id=state["run_id"])


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


async def verify_node(state: AifixState) -> dict[str, Any]:
    """跑全量、过滤抖动、三态判定、按判定 commit 或 rollback。零 LLM。"""
    cfg = state["config"]
    target = state["current"]
    wt = _worktree(state)
    adapters = adapters_from_state(state)
    worktree_path = Path(state["worktree_path"])

    baseline = FailureSet({i: state["_failures"][i]
                           for i in state["baseline_ids"]
                           if i in state["_failures"]})
    # `require_report=True` 两处都不能省，这是判定的取证环节：
    #
    # 全量这一份缺失 → current 是空集合 → 「目标不再失败、也没有新失败」 →
    # 判 BETTER → 一个从未被验证过的补丁被 commit 进交付分支，报告写「已修复」。
    # 「唯一有资格说修好了的是最笨的那个组件」在这里被击穿：最笨的那个没开口，
    # 判定照做了。
    #
    # 复跑那一份缺失更反 → filter_flaky 把空集合读成「重跑就绿」，于是**真的
    # 被这个补丁弄红的用例**全部划进抖动、从 effective 里剔除，判定回到
    # BETTER。不是没验证，是把反证据当成了正证据。
    #
    # 代价是这一轮直接抛出去、run 记成 crash（退出码 1）。这正是想要的：
    # 环境坏了的话后面每一轮都会坏，快速失败比匀速烧钱好。
    current = await run_full_suite(worktree_path, adapters,
                                   require_report=True,
                                   timeout=cfg.test_timeout_seconds)

    # 复跑要知道每条 id 归谁。**两份记账并起来**：要复跑的 id 既可能来自
    # baseline（这一跑里它已经变绿，于是不在 current.owner 里），也可能是这一跑
    # 才冒出来的新失败（baseline 里没有它）。只用其中一份，另一半会落进
    # 「记账里没有它」而抛异常 —— 而那两半都是正常情形，不是错误。
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

    # 一个字节都没改却判 BETTER，说明目标用例在 baseline 里本来就是抖的。
    # 放任不管的话：commit(paths=[]) 是空操作、worktree 随后被删、报告却写
    # 「已修复」—— 系统宣称修好了一个它没碰过的 bug，这会直接击穿
    # 「只有 verify 有资格说修好了」这条主张。
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

    # suspect_anchored 缺省当 True：只有 detect_node 会写这个键，而它写的是
    # 这一轮的真值。默认 False 会让所有绕过 detect 的路径（图那条、测试夹具）
    # 悄悄关掉这一列信号——把「没人告诉我」读成「诊断不可信」是反的。
    sig = analyze({p: (wt.file_at_head(p), _now(p)) for p in touched},
                  suspect=(state.get("diagnosis") or {}).get("suspect_file"),
                  suspect_anchored=state.get("suspect_anchored", True))

    # 必要性反查：逐个 hunk 反向、只重跑目标用例，撤掉之后目标照样绿的报出来。
    # 与静态信号同一层（只给人看、不改判定），但它要**动工作区**，所以位置有
    # 两条硬约束：
    #
    # 1. 必须在 commit **之前**。反查会短暂地把某个 hunk 撤掉再原样写回；放在
    #    commit 之后的话，进程若在这中间死掉，交付分支上已经多了一个提交而工
    #    作区是脏的 —— 放在之前，同样的死法只是「这一轮没交付」。
    # 2. 只在判 BETTER 时跑。判 SAME / WORSE 的补丁马上要被 rollback 掉，问它
    #    「哪些改动是多余的」没有意义；而且那时目标用例本来就是红的，反查的
    #    判据（撤掉之后目标还绿不绿）根本不成立。
    #
    # 任何意外都吞掉：它是一条信号，不能挡住一个已经验证通过的补丁交付。
    # necessity.unnecessary_changes 自己保证工作区逐字还原（连异常路径），所以
    # 这里接住之后可以照常往下走。
    #
    # 新增文件单独传：`git diff` 看不见未跟踪文件，而新建一个源文件是合法的
    # 修复（同一条理由记在 `Worktree.commit` 的 docstring 里）。
    unnecessary: list[str] = []
    if verdict is Verdict.BETTER and cfg.necessity_check:
        try:
            unnecessary = await unnecessary_changes(
                worktree_path, wt.diff(),
                new_files=[p for p in touched
                           if wt.file_at_head(p) is None and (
                               worktree_path / p).is_file()],
                target=target, rerun=_rerun,
                max_units=cfg.necessity_max_units)
        except Exception as e:      # noqa: BLE001 —— 信号不能挡交付
            trace.fact("necessity_check_failed", str(e))

    # commit 提到这里（而不是留在下面的 BETTER 分支里）：**它的返回值参与判
    # 定**，所以必须发生在写 verdict / 信号那几条 fact 之前，否则 facts.jsonl
    # 里会先写下一个随后被推翻的 better，被丢弃的补丁也会被记成交付。
    #
    # 判据是「提交有没有真的产生」，不是提前用 git diff 去猜：这个系统里唯一
    # 有资格回答「分支上有没有东西」的是 git 自己。git diff 看不见未跟踪文件，
    # 而新建一个源文件是完全合法的修复 —— 按 diff 判会把它误降级成 SAME，那
    # 是比多报一次修复更糟的回归（真修复连同报告一起被扔掉）。
    #
    # 上面那道 touched 守卫仍然保留：它更早、更便宜，且在 commit 之前就能挡
    # 住「一个字节都没碰」。这一道管的是另一件事 —— 碰了，但补丁被自己的反向
    # 补丁抵消了，touched 非空而暂存区为空。两者的 fact 分开记：复盘要区分的
    # 是模型的行为，不是判定的结果。
    # 交付失败必须在这里落地，不能让它裸穿上去。`git add -- <路径列表>` 有两
    # 种真实的失败形态（都实测过）：某条路径匹配不到（模型新建了 helper.py，
    # 改完又发补丁把它删掉 —— touched 里还留着它）时 git 退 128 且**一条都不
    # 暂存**；新文件命中 .gitignore 时退 1，而别的路径**已经**暂存了。
    #
    # 让 RuntimeError 上抛的后果不是「报错」而是**失联**：run_once 的 try 里
    # 只有 finally: trace.close()，_cmd_run 也不接，`with Worktree` 退出把
    # worktree 删掉，report_node 根本执行不到 —— 用户拿到一段裸 traceback，
    # report.md 不存在，本次 run 前面几个 failure **已经提交进交付分支**的修复
    # 也没人告诉他。这与「变异撞车时把半小时成果捞出来」是同一类问题的另一侧。
    #
    # 接住之后降级成 SAME 走既有的回滚通路：这一个 failure 没交付成，是事实；
    # 别的 failure 已经交付的提交留在分支上，报告照常产出并写明出了什么事。
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

    # 信号只对**真正交付的补丁**负责。判 BETTER 才写这三条 fact，因为只有
    # 这一支会 commit 进交付分支；SAME / WORSE 的补丁下面就被 rollback 丢
    # 掉了，它从未存在过。否则「第 1 轮删了公开符号被回滚、第 2 轮干净地修
    # 好」会被 eval 记成 fix_hits=1 且 signals≥1 —— 而 eval/score.py 恰好把
    # 这个组合定义为规格套利的指纹，于是指纹是假的，方向还偏向爱试错的模型。
    #
    # 三类**各写一条**，value 是整个列表，不是一个符号一条：每个交付的补丁
    # 至多贡献 3 条，单位是「类」不是「个」。按符号个数展开的话，在一个文件
    # 里删 10 个符号记 10、把改动摊到 20 个文件一个符号没删记 1，跨模型比这
    # 一列就不是同一把尺（规模仍留在 value 与报告里，没有丢）。
    # key 名沿用单数：facts.jsonl 是评测与人共同消费的数据契约，改名会让
    # 历史 run 的 facts 与新代码对不上，收益不抵代价。
    signals = list(state.get("signals") or [])
    if verdict is Verdict.BETTER:
        # 只在有信号时写 fact：一条恒定出现的空 fact 会让 facts.jsonl 变噪音
        if sig.removed_public_symbols:
            trace.fact("removed_public_symbol", sig.removed_public_symbols)
        if sig.new_module_state:
            trace.fact("new_module_state", sig.new_module_state)
        if sig.files_outside_suspect:
            trace.fact("files_outside_suspect", sig.files_outside_suspect)
        # 刻意**不**进 `eval/runner._SIGNAL_KEYS`。那一列的口径是「零模型调用
        # 的静态信号，每个交付的补丁至多 3 条」，而这一条要跑测试、条数没有上
        # 界（一个补丁能报出 max_units 条），掺进去之后跨模型比的就不是同一把
        # 尺了 —— 而且历史 run 的 facts 里没有它，新旧数据也不再可比。
        # 它先以独立的一条存在，攒够数据再决定要不要并进那一列。
        if unnecessary:
            trace.fact("unnecessary_hunk", unnecessary)
        if not sig.is_empty() or unnecessary:
            # 带上 test_id：多 failure 的 run 里，报告只给一份并集的话，人分
            # 不清是哪一次改动删的符号。这个 key 是**追加**不是替换 ——
            # 核心循环对每个 failure 各跑一轮 verify，替换只会剩最后一轮。
            signals.append({"test_id": target, **asdict(sig),
                            "unnecessary_hunks": unnecessary})
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
                "consecutive_failures": 0, **common}

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
                **common}

    return {"verdict": verdict.value, "attempt": state["attempt"] + 1,
            "results": results, "diagnosis": None, **common}
