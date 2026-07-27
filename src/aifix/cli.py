from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path
from typing import Any

from .config import AifixConfig
from .delivery import Worktree
from .graph import AifixState, check_circuit_breaker, new_state
from .nodes.baseline import baseline_node
from .nodes.detect import detect_node
from .nodes.fix import fix_node
from .nodes.preflight import preflight_node
from .nodes.report import render_report
from .nodes.verify import verify_node


async def run_once(repo: Path, config: AifixConfig, run_id: str,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> AifixState:
    """按状态图的语义顺序执行一次完整 run。

    M1 直接手工驱动节点，语义与 build_graph() 的图完全一致——把
    LangGraph 的 checkpointer 接进来是 M2 的事（需要先有 trace 落盘）。
    """
    state = new_state(repo, config, run_id=run_id)
    state.update(preflight_node(state))
    if state["abort"]:
        state["report_md"] = render_report(state)
        return state

    with Worktree(repo, run_id=run_id) as wt:
        state["worktree_path"] = str(wt.path)
        state["branch"] = wt.branch

        # 全量测试很贵，整个 run 只在这里跑一次；后续每轮 verify 各跑一次
        state.update(await baseline_node(state))

        while True:
            if state["current"] is None:
                if state["abort"] or not state["queue"]:
                    break
                state["current"] = state["queue"].pop(0)
                state["attempt"] = 1
            state.update(await detect_node(state, client=detector_client))
            state.update(await fix_node(state, client=fixer_client))
            state.update(await verify_node(state))
            tripped = check_circuit_breaker(state)
            if tripped:
                state["abort"] = tripped
                break

    state["report_md"] = render_report(state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(prog="aifix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="修复当前 repo 的失败测试")
    run.add_argument("repo", nargs="?", default=".")
    args = parser.parse_args()

    if args.cmd == "run":
        state = asyncio.run(run_once(
            Path(args.repo).resolve(), AifixConfig(), run_id=uuid.uuid4().hex[:8]))
        print(state["report_md"])
