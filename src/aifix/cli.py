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
from .nodes.baseline import baseline_node
from .nodes.detect import detect_node
from .nodes.fix import fix_node
from .nodes.preflight import preflight_node
from .nodes.report import render_report, report_node
from .nodes.verify import verify_node
from .trace import RunTrace


async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None,
                   only_test: str | None = None,
                   dry_run: bool = False) -> AifixState:
    """按状态图的语义顺序执行一次完整 run。

    M1 直接手工驱动节点，语义与 build_graph() 的图完全一致——把
    LangGraph 的 checkpointer 接进来是 M2 的事（需要先有 trace 落盘）。
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
                    break
                # 剩余 failure 数 = 队列里的 + 手上这个
                state["failure_token_budget"] = budget.for_failure(
                    len(state["queue"]) + 1)
                state["failure_usd_budget"] = budget.usd_for_failure(
                    len(state["queue"]) + 1)
                before = state["spent_tokens"], state["spent_usd"]
                with trace.failure_span(state["current"]), \
                        trace.attempt_span(state["attempt"]):
                    state.update(await detect_node(state, client=detector_client))
                    state.update(await fix_node(state, client=fixer_client))
                    budget.charge(state["spent_tokens"] - before[0],
                                  state["spent_usd"] - before[1])
                    state.update(await verify_node(state))
                tripped = check_circuit_breaker(state)
                if tripped:
                    state["abort"] = tripped
                    trace.fact("abort", tripped)
                    break

        state.update(report_node(state))
    finally:
        trace.close()
    return state


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aifix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="修复当前 repo 的失败测试")
    run.add_argument("repo", nargs="?", default=".")
    run.add_argument("--test", default=None,
                     help="只修这一个失败用例（test_id）")
    run.add_argument("--budget", type=float, default=None,
                     help="本次 run 的美元上限。语义是「越线之后不再发起新的"
                          "模型调用」——成本只有在调用返回后才知道，所以最后"
                          "那一次必然已经花掉。需要配置 AIFIX_PRICE_MAP")
    run.add_argument("--dry-run", action="store_true",
                     help="只跑 preflight + baseline，报告有多少活")

    mine = sub.add_parser("mine", help="从 git history 挖任务集")
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

    rep = sub.add_parser("eval-report", help="把若干轮结果渲染成对比表")
    rep.add_argument("results", nargs="+")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "run":
        _cmd_run(args)
    elif args.cmd == "mine":
        _cmd_mine(args)
    elif args.cmd == "eval":
        _cmd_eval(args)
    elif args.cmd == "eval-report":
        _cmd_eval_report(args)


def _cmd_run(args) -> None:
    config = AifixConfig()
    if args.budget is not None:
        config = config.model_copy(update={"budget_usd": args.budget})
    require_price_map_for_usd_budget(config)
    state = asyncio.run(run_once(
        Path(args.repo).resolve(), config, run_id=uuid.uuid4().hex[:8],
        only_test=args.test, dry_run=args.dry_run))
    print(state["report_md"])


def _cmd_mine(args) -> None:
    # 延迟导入：eval 子包依赖 cli 模块（run_once），提到模块顶部会形成循环导入
    from .adapters.pytest_adapter import PytestAdapter
    from .eval.mine import mine_tasks
    from .eval.task import write_jsonl

    def progress(sha: str, n: int, error: str | None = None) -> None:
        if error is not None:
            # 与「0 个可用用例」分开显示：前者要去查，后者是正常结果
            print(f"  {sha[:8]}：验证失败，已跳过 —— {error}", flush=True)
        else:
            print(f"  {sha[:8]}：{n} 个可用用例", flush=True)

    tasks = asyncio.run(mine_tasks(
        str(Path(args.repo).resolve()), PytestAdapter(),
        limit=args.limit, max_tasks=args.max_tasks, on_progress=progress))
    write_jsonl(Path(args.out), tasks)
    print(f"产出 {len(tasks)} 个任务 → {args.out}")


def _cmd_eval(args) -> None:
    # 延迟导入：理由同上
    from .eval.runner import run_suite
    from .eval.score import render_table, summarize
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
    print(render_table([summarize(results)]))
    print(f"明细 → {out}")
    shutil.rmtree(workdir, ignore_errors=True)


def _cmd_eval_report(args) -> None:
    # 延迟导入：理由同上
    from .eval.score import render_table, summarize
    from .eval.task import TaskResult, read_jsonl

    summaries = [summarize(read_jsonl(Path(p), TaskResult))
                 for p in args.results]
    print(render_table(summaries))
