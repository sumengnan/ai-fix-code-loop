from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .budget import RunBudget
from .config import AifixConfig
from .delivery import Worktree
from .graph import AifixState, check_circuit_breaker, new_state
from .nodes.baseline import COLLECTION_ABORT_KIND, baseline_node
from .nodes.detect import detect_node
from .nodes.fix import fix_node
from .nodes.preflight import preflight_node
from .nodes.report import (cost_is_unknown, count_fixed, render_report,
                           report_node)
from .nodes.verify import verify_node
# replay 与 trajectory 可以放在模块顶部：它们只读 jsonl / sqlite，一个
# aifix 内部模块都不 import，不存在 eval 子包那条 `eval.runner → cli` 的环
# （trajectory 宁可复制一份 SIGNAL_KEYS 也不 import eval.runner，正是为此）。
from .replay import render as render_replay
from .trace import RunTrace
from .trajectory import DB_RELPATH, ingest, query_stats


def _backfill_in_flight_result(state: AifixState) -> None:
    """预算耗尽 / 熔断中止时，把手上正在处理的 failure 补录进 results。

    verify_node 只在两种终局分支写 results 行：判定为 BETTER，或
    attempt 已到 max_attempts。若某一轮没修好，verify_node 只会把
    attempt 递增为「下一轮的编号」然后返回，不落任何记录——这本是
    对的（它还要继续重试）。但如果主循环恰好在下一轮开头发现预算
    耗尽或连续失败熔断而直接 break，这个 failure 就再也没有机会
    走到 verify_node 的任何一个终局分支，于是从 results 里彻底消失。
    这个用例真的跑过、真的花了钱、真的没修好——报告却显示「压根
    没轮到它」，用户没法区分这两种情况，而后者恰恰是「没记录」
    应该表达的含义。所以中止发生时要在这里把它补上。
    """
    current = state.get("current")
    if not current:
        return
    if any(r.get("test_id") == current for r in state["results"]):
        return
    # attempt == 1 表示这个 failure 刚从队列 pop 出来、detect/fix/verify 一次
    # 都没跑 —— 主循环是「先 pop 下一个、再查预算」的顺序，所以中止完全可能
    # 落在这个空档里。只有 verify_node 的非终局分支才会把 attempt 推到 >= 2。
    # 不挡住的话，下面会拿**上一个** failure 的 verdict 给它记一笔：上一个判
    # BETTER 时，这个一个字节没改、花了 $0 的 failure 会被记成「已修复」。
    # 后果有两层——报告谎报修复数；eval 的 run_task 按 test_id 取到这一行，
    # 评测的修复成功率也跟着虚高。
    if state.get("attempt", 0) < 2:
        return
    verdict = state.get("verdict") or "same"
    # verify_node 的非终局分支把 attempt 递增为下一轮编号，真实跑过的
    # 轮数是 attempt - 1；上面的闸已保证 attempt >= 2，无需再兜底
    attempts = state["attempt"] - 1
    state["results"].append({
        "test_id": current, "verdict": verdict,
        "attempts": attempts, "abort_reason": state.get("abort"),
    })


async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None,
                   only_test: str | None = None,
                   dry_run: bool = False) -> AifixState:
    """按状态图的语义顺序执行一次完整 run。

    M1 直接手工驱动节点，节点顺序与路由和 build_graph() 的图一致——把
    LangGraph 的 checkpointer 接进来是 M2 的事（需要先有 trace 落盘）。

    **但两条路径不再等价：预算只在这里**。RunBudget、`failure_token_budget`
    与 `failure_usd_budget` 的分配、以及「越线即中止」的检查全部写在这个
    函数里；build_graph() 那条路径没有 RunBudget，`failure_usd_budget`
    一直是 None，整条美元闸不存在。产品入口走的是 run_once，图那条路径
    目前只用于结构验证，别拿它去验证任何与花钱有关的保证。
    """
    state = new_state(repo, config, run_id=run_id)
    state.update(preflight_node(state))
    if state["abort"]:
        state["report_md"] = render_report(state)
        return state

    artifact_dir = Path(repo) / ".aifix" / "runs" / run_id
    state["artifact_dir"] = str(artifact_dir)
    trace = RunTrace(artifact_dir, run_id=run_id)
    state["_trace"] = trace

    try:
        with Worktree(repo, run_id=run_id) as wt, trace.run_span():
            state["worktree_path"] = str(wt.path)
            state["branch"] = wt.branch

            # 全量测试很贵，整个 run 只在这里跑一次；后续每轮 verify 各跑一次
            state.update(await baseline_node(state))
            trace.fact("baseline_failures", len(state["baseline_ids"]))
            if only_test is not None:
                state["queue"] = [t for t in state["queue"] if t == only_test]
            if dry_run:
                # 不调用任何模型：接一个陌生项目时先看清工作量
                trace.fact("dry_run", True)
                state["queue"] = []

            budget = RunBudget(total_tokens=config.budget_tokens,
                               total_usd=config.budget_usd,
                               total_seconds=config.budget_wall_seconds)
            budget.start()

            while True:
                if state["current"] is None:
                    if state["abort"] or not state["queue"]:
                        break
                    state["current"] = state["queue"].pop(0)
                    state["attempt"] = 1
                spent = budget.exhaustion()
                if spent:
                    # 种类另记一份：评测要据此区分「模型没在预算内修好」
                    # 与「评测调度器的墙钟耗尽」，光靠消息文本判不可靠
                    state["abort_kind"], state["abort"] = spent
                    trace.fact("abort", state["abort"])
                    trace.fact("abort_kind", state["abort_kind"])
                    _backfill_in_flight_result(state)
                    break
                # 剩余 failure 数 = 队列里的 + 手上这个
                remaining_failures = len(state["queue"]) + 1
                state["failure_token_budget"] = budget.for_failure(
                    remaining_failures)
                before = state["spent_tokens"], state["spent_usd"]
                with trace.failure_span(state["current"]), \
                        trace.attempt_span(state["attempt"]):
                    state.update(await detect_node(state, client=detector_client))
                    # detect 不接美元闸是计划登记过的有意偏差，但它花掉的钱
                    # 必须**在这里立刻结算**：否则下面算给 fix 的额度是按
                    # 「detect 还没花钱」算出来的，fix 会在可能已经越线的
                    # 状态下发起新调用，「越线之后不再发起新的模型调用」这句
                    # 话就不成立 —— 实测 budget_usd=20、单次调用 $15、单
                    # failure 会花掉 $45，超支 1.67 次调用而不是一次。
                    budget.charge(state["spent_tokens"] - before[0],
                                  state["spent_usd"] - before[1])
                    mid = state["spent_tokens"], state["spent_usd"]
                    # 额度必须在 detect 结算之后才算，取的是扣掉 detect 之后
                    # 的剩余。算出 0.0 是正常结果（detect 恰好花光），fix_node
                    # 按 is None 判定，会把它当成扣光的闸而不是「没设闸」。
                    state["failure_usd_budget"] = budget.usd_for_failure(
                        remaining_failures)
                    state.update(await fix_node(state, client=fixer_client))
                    # 两笔 charge 相加恰好是 after_fix - before：不重复计入，
                    # 也不遗漏。verify_node 零 LLM，不产生花销。
                    budget.charge(state["spent_tokens"] - mid[0],
                                  state["spent_usd"] - mid[1])
                    state.update(await verify_node(state))
                tripped = check_circuit_breaker(state)
                if tripped:
                    state["abort"] = tripped
                    trace.fact("abort", tripped)
                    _backfill_in_flight_result(state)
                    break

    except Exception as e:
        # 报告是用户手里**唯一**的成果凭据：worktree 退出时就被删了，分支上
        # 有没有东西、修好了几个、下一步该干什么，全写在报告里。异常裸穿出去
        # 的后果不是「报错」而是失联 —— 这次 run 前面几个 failure 可能已经把
        # 修复提交进交付分支了，而用户只看到一段调用栈。
        #
        # 所以这里不吞异常、只保证报告先落地：记成一次中止（abort_kind
        # 另记，评测据此把它算作评测故障而不是模型没修好），渲染、落盘，
        # 然后照常返回。退出码由 _cmd_run 负责，管道里区分得出来。
        state["abort"] = state.get("abort") or f"运行异常中断：{type(e).__name__}：{e}"
        state["abort_kind"] = state.get("abort_kind") or "crash"
        trace.fact("crash", f"{type(e).__name__}: {e}")
        trace.fact("abort", state["abort"])
        trace.fact("abort_kind", "crash")
        _backfill_in_flight_result(state)
        _record_run_summary(trace, state)
        state.update(report_node(state))
        return state
    else:
        _record_run_summary(trace, state)
        state.update(report_node(state))
    finally:
        trace.close()
    return state


def _record_run_summary(trace: RunTrace, state: AifixState) -> None:
    """把 run 的收尾数据落成事实：适配器、分支、修复数、花销。

    这四类值以前只存在于渲染出来的 report.md 里，跨 run 的轨迹表只能拿正则
    去解自己渲染的 markdown —— 报告改一个字，那几列就静默变成 NULL，而聚合
    查询照跑不误、给出的每个数都是「看着正常」的错数。事实是数据契约，报告
    是渲染，两者不该反过来。

    写在 report_node 之前：报告本身会因为字段缺失而少印一行，事实不会。
    """
    trace.fact("adapter", state["adapter_name"])
    trace.fact("branch", state["branch"])
    trace.fact("fixed", count_fixed(state["results"]))
    tokens, usd = state["spent_tokens"], state["spent_usd"]
    trace.fact("spent_tokens", tokens)
    # 没配价格表时成本恒为 0，落库存 0.0 就是伪造 $0.00。写 None 是**有意的**
    # 事实：「花了钱，但这次不知道花了多少」——与「真的没花钱」是两回事。
    trace.fact("spent_usd", None if cost_is_unknown(tokens, usd) else usd)


_LABEL_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)


def _safe_label(label: str) -> str:
    """把模型标签洗成可安全用作文件名的形式。

    模型名常带斜杠（org/model-v2）。直接拼进 f"evals/results-{label}.jsonl"
    会被 Path 当成子目录，而 write_jsonl 会 mkdir(parents=True) —— 于是
    不报错，只是静默写到别处；下一轮换个不带斜杠的模型名，两轮结果就
    分散在不同目录，跨模型对比再也对不上。
    """
    return _LABEL_UNSAFE.sub("_", label).strip("_") or "未命名"


def require_price_map_for_usd_budget(config: AifixConfig,
                                     also_explicit: bool = False) -> None:
    """显式要求了美元上限却没有价格表 —— 当场拒绝。

    also_explicit：给 `eval --budget-total` 用。那个上限不进配置对象
    （它是这一次调用的调度约束，不是项目配置），所以 model_fields_set
    看不见它，得由调用方把「用户确实要求了美元闸」这件事带进来。

    不配价格表时 effective_cost 恒为 0，美元闸永远不会触发：用户设了上限，
    系统欣然接受，然后一分钱不拦。与其给一个假的保证，不如现在就停。

    「显式」由 pydantic 的 model_fields_set 判定，默认值不在其中；CLI 的
    --budget 走 model_copy(update=...)，同样会被记住，所以一处判定管住
    环境变量、构造参数、命令行三条来源。
    """
    explicit = also_explicit or "budget_usd" in config.model_fields_set
    if not explicit or config.price_map:
        return
    raise SystemExit(
        "拒绝启动：设置了美元预算上限，但没有配置价格表，这个上限不会生效。\n"
        "  没有 price_map 时成本恒为 0，闸永远不触发 —— 与其给一个假的保证，"
        "不如现在就停。\n"
        "  修法一：配置价格表（每千 token 的 [输入价, 输出价]）\n"
        "    export AIFIX_PRICE_MAP='{\"deepseek-v4-pro\": [0.003, 0.006]}'\n"
        "  修法二：去掉美元上限，改用 AIFIX_BUDGET_TOKENS 限制 token")


# run / mine / mutate 三个子命令都在目标项目里真跑测试，都吃这一段。
_TEST_PYTHON_HELP = """\
测试用哪个解释器（AIFIX_TEST_PYTHON）：
  显式配置 > 源仓库里的 .venv/bin/python 或 venv/bin/python > aifix 自己的解释器。
  目标项目的测试依赖装在它自己的环境里，而不是 aifix 的环境里 —— 不这样做的话，
  拿 aifix 的解释器去跑别人的测试通常只会得到一堆 collection error。

  **一个必须知道的陷阱**：目标项目如果把自己可编辑安装（pip install -e .）进了
  那个解释器，`import <目标包>` 可能解析到**源仓库**，而不是 worktree 里那份打了
  补丁的代码 —— 测试照跑照绿，而验证的是没打补丁的代码，结论是假的。
  aifix 在 baseline 之前会做一次近似探测并往 stderr 出声，但那是提醒不是保证：
  它复现不了 conftest.py 里手写的 sys.path 改动。最可靠的自保是在目标项目的
  pytest 配置里设 pythonpath（如 [tool.pytest.ini_options] pythonpath = ["src"]），
  它会把 worktree 的源码目录插到 sys.path 最前，盖过可编辑安装那条记录。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aifix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser(
        "run", help="修复当前 repo 的失败测试", epilog=_TEST_PYTHON_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("repo", nargs="?", default=".")
    run.add_argument("--test", default=None,
                     help="只修这一个失败用例（test_id）")
    run.add_argument("--budget", type=float, default=None,
                     help="本次 run 的美元上限。语义是「越线之后不再发起新的"
                          "模型调用」——成本只有在调用返回后才知道，所以最后"
                          "那一次必然已经花掉。需要配置 AIFIX_PRICE_MAP")
    run.add_argument("--dry-run", action="store_true",
                     help="只跑 preflight + baseline，报告有多少活")

    mine = sub.add_parser(
        "mine", help="从 git history 挖任务集", epilog=_TEST_PYTHON_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mine.add_argument("repo", nargs="?", default=".")
    mine.add_argument("--limit", type=int, default=50,
                      help="回溯多少个提交")
    mine.add_argument("--max-tasks", type=int, default=10,
                      help="最多产出多少个任务")
    mine.add_argument("--out", default="evals/tasks.jsonl")

    ev = sub.add_parser("eval", help="在任务集上跑评测")
    ev.add_argument("tasks")
    ev.add_argument("--parallel", type=int, default=4)
    ev.add_argument("--label", default=None,
                    help="这一轮的模型标签，默认取 fixer 的 model")
    ev.add_argument("--out", default=None)
    ev.add_argument("--budget-per-task", type=float, default=None,
                    help="每个任务的美元上限")
    ev.add_argument("--budget-total", type=float, default=None,
                    help="整批的美元上限。检查发生在派发时按并发上限预留额度，"
                         "所以整批超支上界 = 并发数 × 一次模型调用的成本，"
                         "而不是随任务数线性放大")

    mut = sub.add_parser(
        "mutate", help="人造变异生成冒烟任务集",
        description="产出的是冒烟集，不是基准。变异的分布与真实 bug 不同——"
                    "它便宜、确定、可任意规模，用来验证链路本身是否工作；"
                    "拿它跨模型比高低是过度解读。",
        epilog=_TEST_PYTHON_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mut.add_argument("repo", nargs="?", default=".")
    mut.add_argument("--max-tasks", type=int, default=10,
                     help="最多产出多少个任务")
    mut.add_argument("--max-new-failures", type=int, default=5,
                     help="一个变异最多允许弄红几个用例，超过即丢弃。"
                          "注意作用域：--scope smart 下它只约束**词干匹配到的"
                          "那几个测试文件内**的新失败数，全仓可能红得更多；"
                          "只有 --scope full 才是全仓口径")
    mut.add_argument("--scope", choices=["smart", "full"], default="smart",
                     help="smart 只跑与被变异文件词干相关的测试文件（快，会漏，"
                          "漏了只是少一个候选，不产生假任务）；full 每个变异跑一次全量")
    mut.add_argument("--seed", type=int, default=0,
                     help="变异选点的随机种子，固定后同一个仓库产出可复现")
    mut.add_argument("--out", default="evals/tasks-mutants.jsonl")

    rep = sub.add_parser("eval-report", help="把若干轮结果渲染成对比表")
    rep.add_argument("results", nargs="+")

    rp = sub.add_parser(
        "replay", help="回放一次 run 的逐步复盘",
        description="把一次 run 落下的 events.jsonl / facts.jsonl 渲染成可读的"
                    "时间轴。run_id 不存在时会列出这个仓库里现有的 run —— "
                    "诊断工具的第一要务是让人找得到东西。")
    rp.add_argument("run_id")
    rp.add_argument("--repo", default=".")
    rp.add_argument("--step", type=int, default=None,
                    help="只看第 N 步。用的是全局步号（从 1 数起）：一次 run 会开"
                         "好几段 AgentLoop，每段内部的步号都从 1 重新数，这里按"
                         "全局顺序重新编过，与单段会话里的步号对不上")
    rp.add_argument("--full", action="store_true",
                    help="不截断长文本（补丁、工具返回常有几千字）。默认每个字段"
                         "截到 2000 字符，截断处一定留标记")

    ing = sub.add_parser(
        "ingest", help="把各次 run 的事实灌进 .aifix/trajectory.db",
        description="扫 <repo>/.aifix/runs/*/facts.jsonl 落库，供 aifix stats 查询。"
                    "run 结束时不自动灌库：那等于给核心循环加一条可能失败的写路径，"
                    "而这张表是事后诊断用的，晚几分钟没有代价。"
                    "幂等 —— 同一批产物灌任意多次，表里的行数不变。")
    ing.add_argument("--repo", default=".")

    st = sub.add_parser(
        "stats", help="跨 run 汇总：适配器、守卫、可疑信号",
        description="数据来自 .aifix/trajectory.db，不会自己更新，先跑一次 "
                    "aifix ingest。空表不代表没跑过 run，只代表没灌过库。")
    st.add_argument("--repo", default=".")
    return parser


def _dispatch() -> dict[str, Any]:
    """子命令名 → 处理函数。

    单独拿出来是为了能被整表比对：加了 parser 却忘了在这里接一行的话，
    argparse 照样解析成功、一个分支都不匹配、退出码 0 —— 命令静默什么都不做，
    这种失败在手工试用时最容易被当成「跑了但没输出」。
    """
    return {"run": _cmd_run, "mine": _cmd_mine, "mutate": _cmd_mutate,
            "eval": _cmd_eval, "eval-report": _cmd_eval_report,
            "replay": _cmd_replay, "ingest": _cmd_ingest, "stats": _cmd_stats}


def main() -> None:
    args = build_parser().parse_args()
    _dispatch()[args.cmd](args)


def _cmd_run(args) -> None:
    config = AifixConfig()
    if args.budget is not None:
        config = config.model_copy(update={"budget_usd": args.budget})
    require_price_map_for_usd_budget(config)
    state = asyncio.run(run_once(
        Path(args.repo).resolve(), config, run_id=uuid.uuid4().hex[:8],
        only_test=args.test, dry_run=args.dry_run))
    print(state["report_md"])
    if state.get("abort_kind") in ("crash", COLLECTION_ABORT_KIND):
        # 报告先印出来（分支上可能真躺着可合并的修复），退出码再说明这次
        # 是崩的：退 0 的话流水线里「跑完了」和「炸了但报告还在」没有区别。
        #
        # 收集错误中止同样退非 0，理由是同一条：这次**没测成**。预算耗尽
        # （tokens / usd / wall）相反，那是正常收场 —— 活干到钱花完为止，
        # 结论仍然可信，所以仍退 0。
        raise SystemExit(1)


def _cmd_mine(args) -> None:
    # 延迟导入：eval 子包依赖 cli 模块（run_once），提到模块顶部会形成循环导入
    from .adapters.pytest_adapter import resolve_test_python
    from .eval.mine import mine_tasks
    from .eval.task import write_jsonl
    from .nodes.baseline import detect_adapter

    repo = Path(args.repo).resolve()
    # 挖任务要在克隆出来的工作树里真跑测试，和 run 一样吃「用哪个解释器」这个
    # 问题。不接这一行的话，`aifix run` 能跑起来的项目 `aifix mine` 照样跑不动，
    # 而失败形态是「0 个可用用例」—— 与「这个仓库最近没有红转绿的提交」一模一样。
    test_python = resolve_test_python(repo, AifixConfig().test_python)
    # 适配器要探测，不能写死。这里曾是 `PytestAdapter()`：Maven 仓库拿到的
    # source_suffixes() 只认 `.py`，gold_files 恒空，产出 0 个任务且不报错 ——
    # 与「这个仓库最近没有红转绿的提交」无法区分。走 preflight 用的同一份
    # 探测，新增适配器时不会漏掉这一处。
    adapter = detect_adapter(repo, python=test_python)
    if adapter is None:
        print(f"没有适配器认领这个项目：{repo}")
        raise SystemExit(1)

    def progress(sha: str, n: int, error: str | None = None) -> None:
        if error is not None:
            # 与「0 个可用用例」分开显示：前者要去查，后者是正常结果
            print(f"  {sha[:8]}：验证失败，已跳过 —— {error}", flush=True)
        else:
            print(f"  {sha[:8]}：{n} 个可用用例", flush=True)

    tasks = asyncio.run(mine_tasks(
        str(repo), adapter,
        limit=args.limit, max_tasks=args.max_tasks, on_progress=progress))
    write_jsonl(Path(args.out), tasks)
    print(f"产出 {len(tasks)} 个任务 → {args.out}")


def _cmd_mutate(args) -> None:
    # 延迟导入：eval 子包依赖 cli 模块（run_once），提到模块顶部会形成循环导入
    from .adapters.pytest_adapter import PytestAdapter, resolve_test_python
    from .eval.mutate import DuplicateTaskIds, mutate_tasks
    from .eval.task import write_jsonl

    repo = Path(args.repo).resolve()
    # 同 _cmd_mine：变异要在克隆的工作树里真跑测试来验证候选，用错解释器时
    # 每个候选都「验证失败」，产出 0 个任务。
    test_python = resolve_test_python(repo, AifixConfig().test_python)

    def progress(rel_path: str, n: int, error: str | None = None) -> None:
        if error is not None:
            print(f"  {rel_path}：变异失败，已跳过 —— {error}", flush=True)
        else:
            print(f"  {rel_path}：{n} 个可用任务", flush=True)

    try:
        tasks = asyncio.run(mutate_tasks(
            str(repo), PytestAdapter(python=test_python),
            max_tasks=args.max_tasks, max_new_failures=args.max_new_failures,
            scope=args.scope, seed=args.seed, on_progress=progress))
    except DuplicateTaskIds as e:
        # 撞车仍然是错误：撞车的任务集在评测里会静默退化成「评测故障」。
        # 但验证一个候选要真跑一遍测试，一轮变异跑掉半小时是常事 —— 让异常
        # 裸穿出去，下面的 write_jsonl 执行不到，半小时的成果一个不落盘，
        # 屏幕上还只有一段调用栈。
        #
        # 捞出来的那份**绝不能落在 args.out**：用户下一步就是
        # `aifix eval --tasks <out>`，那正好是这道自检要挡的事。旁落
        # `.partial` 并在报错里指路，是让人能接着救、又不会误用。
        partial = Path(str(args.out) + ".partial")
        write_jsonl(partial, e.tasks)
        raise SystemExit(
            f"变异中止：{e}\n"
            f"  已验证的 {len(e.tasks)} 个任务捞到了 {partial}"
            "（**不是**可用的任务集，去重或修掉撞车之后才能拿去 eval）\n"
            f"  {args.out} 一个字节都没写。")
    write_jsonl(Path(args.out), tasks)
    print(f"产出 {len(tasks)} 个冒烟任务 → {args.out}")


def _cmd_eval(args) -> None:
    # 延迟导入：理由同上
    from .eval.runner import run_suite
    from .eval.score import render_table, summarize_by_origin
    from .eval.task import Task, TaskResult, read_jsonl, write_jsonl

    config = AifixConfig()
    if args.budget_per_task is not None:
        config = config.model_copy(
            update={"budget_usd": args.budget_per_task})
    require_price_map_for_usd_budget(
        config, also_explicit=args.budget_total is not None)
    label = args.label or config.fixer.model or "未命名"
    tasks = read_jsonl(Path(args.tasks), Task)
    workdir = Path(tempfile.mkdtemp(prefix="aifix-eval-"))

    def done(r: TaskResult) -> None:
        mark = "✅" if r.verdict == "better" else ("⚠️" if r.error else "❌")
        print(f"  {mark} {r.task_id}", flush=True)

    print(f"{len(tasks)} 个任务 · {label} · 并行 {args.parallel}")
    results = asyncio.run(run_suite(tasks, config, label, workdir,
                                    parallel=args.parallel, on_done=done,
                                    total_usd=args.budget_total))
    out = Path(args.out or f"evals/results-{_safe_label(label)}.jsonl")
    write_jsonl(out, results)
    print()
    # 一份任务集可能混了挖掘（mined）与人造变异（mutated）两种来源，
    # 分布不同，成功率平均成一个数字会失真——按来源拆成多行。
    print(render_table(summarize_by_origin(results)))
    print(f"明细 → {out}")
    shutil.rmtree(workdir, ignore_errors=True)


def _cmd_eval_report(args) -> None:
    # 延迟导入：理由同上
    from .eval.score import render_table, summarize_by_origin
    from .eval.task import TaskResult, read_jsonl

    summaries: list = []
    for p in args.results:
        # 同一个结果文件也可能混了两种来源，逐份拆开后再拼到一起，
        # 不能像老版本那样每份文件只出一行汇总。
        summaries.extend(summarize_by_origin(read_jsonl(Path(p), TaskResult)))
    print(render_table(summaries))


def _cmd_replay(args) -> None:
    run_dir = Path(args.repo).resolve() / ".aifix" / "runs" / args.run_id
    # 「目录不存在」交给 render 自己说：它已经会给人话并列出同级现有的 run。
    # 在这里再判一次就是两套措辞，早晚有一套会跟另一套说得不一样。
    print(render_replay(run_dir, step=args.step,
                        full=args.full).rstrip("\n"))
    if not run_dir.is_dir():
        # 措辞只有 render 那一份，这里只补一个退出码：退 0 的话流水线里
        # 「run_id 打错了」和「回放成功」没有任何区别
        raise SystemExit(1)


def _cmd_ingest(args) -> None:
    repo = Path(args.repo).resolve()
    n = ingest(repo)
    if not n:
        # 与「灌了 0 个」分开说：这里其实是没找到产物，多半是 --repo 指错了
        print(f"没有可灌的 run：{repo / '.aifix' / 'runs'} 下"
              "没有带 facts.jsonl 的目录。")
        return
    # 报的是**本次处理**的 run 数，不是新增数：灌库幂等，重灌同一批仍报同一个
    # 数字，这正是「重灌安全」看得见的样子
    print(f"已灌库 {n} 个 run → {repo / DB_RELPATH}")


def _cmd_stats(args) -> None:
    repo = Path(args.repo).resolve()
    db = repo / DB_RELPATH
    if not db.is_file():
        # 渲染一张空表会被读成「这个仓库没跑过 run」，而事实是没灌过库。
        # query_stats 对不存在的库返回空结果正是为了不顺手建一个空库出来，
        # 那份克制在这里得配一句人话，否则用户只看到三个空小节。
        raise SystemExit(
            f"还没有灌过库：{db} 不存在。\n"
            f"  先跑 `aifix ingest --repo {args.repo}`，再回来看统计。")
    print(_render_stats(query_stats(db), db))


def _fmt_fixed(row: dict[str, Any]) -> str:
    """一组 run 的修复数，**带完整性标注**。

    三种形状，不能合并成一个数字：
    - 全都解得出 → 合计就是合计
    - 一个都解不出 → 破折号；写 0 等于替库里没有的数据下「一个都没修好」的结论
    - 混着 → 最阴的一种。SQL 的 sum() 跳过 NULL，「1 次修好 2 个 + 2 次不知道」
      聚合出来是 2，与「3 次一共修好 2 个」逐字节相同。读的人拿到一个看着正常
      的假数字，而且没有任何线索能发现它是假的 —— 所以必须把「有几行取不到」
      一并印出来，而不是只给一个求和值。
    """
    fixed, unknown = row.get("fixed"), row.get("unknown") or 0
    runs = row.get("runs")
    if fixed is None:
        return f"修复 —（{runs} 次 run 都取不到修复数）"
    if unknown:
        return (f"修复 ≥{fixed} 个用例"
                f"（不完整：{runs} 次 run 里有 {unknown} 次取不到修复数）")
    return f"修复 {fixed} 个用例"


def _render_stats(stats: dict[str, Any], db: Path) -> str:
    """把 query_stats 的三张小结渲染成人能读的中文。

    取不到的字段一律「未知」/「—」，绝不写 0：报告里解不出修复数（库里是
    NULL）与「一个都没修好」（0）是两回事，后者是结论，而我们手上没有这个
    结论。这个项目在成本、修复数上反复吃过「假的 0」的亏。
    """
    hr = "──"
    lines = [f"aifix 跨 run 统计 · {db}", "", f"{hr} 按适配器 {hr}"]
    by_adapter = stats.get("by_adapter") or {}
    if not by_adapter:
        lines.append("  （库里还没有 run 记录）")
    for adapter, row in by_adapter.items():
        lines.append(f"  {adapter or '未知'}：run {row.get('runs')} 次 · "
                     + _fmt_fixed(row))

    lines += ["", f"{hr} 守卫触发（按次数降序）{hr}"]
    hits = stats.get("guard_hits") or []
    if not hits:
        lines.append("  （这批 run 里没有守卫触发的记录）")
    for kind, n in hits:
        # kind 已经由 query_stats 解过 JSON 了。库里存的是带引号的
        # `"empty_diff"`，那是内部编码，不该出现在人看的输出里；反过来在这
        # 儿再解一次也不行 —— empty_diff 不是合法 JSON。
        lines.append(f"  {kind}：{n} 次")

    lines += ["", f"{hr} 可疑信号最多的 run {hr}"]
    signal_runs = stats.get("signal_runs") or []
    if not signal_runs:
        lines.append("  （没有 run 记录到可疑信号）")
    for run_id, n in signal_runs:
        lines.append(f"  {run_id}：{n} 条")
    return "\n".join(lines)
