from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.sandbox.local import LocalSandbox

from ..adapters.junit import parse_junit
from ..adapters.pytest_adapter import PytestAdapter
from ..graph import AifixState

_ADAPTERS = {"pytest": PytestAdapter}


def adapter_for(name: str) -> PytestAdapter:
    return _ADAPTERS[name]()


def _check_report(worktree: Path, paths: list[Path], required: bool) -> None:
    """required 时至少要有一份报告，否则抛 —— 「没跑成」不能冒充「跑完了、全绿」。

    parse_junit 对缺失报告的处理是安全跳过并返回空集合。对核心循环这是对的
    （少一份报告不该让整个 run 崩掉，下一轮 verify 会重新跑）；但对挖任务是
    致命的：空集合会被读成「全绿」，`red - green` 于是把 base 处所有红的用例
    全部当成「红转绿」吐出来。报告缺失的真实含义是超时被杀 / 进程崩溃 /
    沙箱执行失败，这时唯一安全的动作是让调用方知道并跳过。

    消息里不点名具体文件：报告可以有多份（surefire 每个测试类一份），
    点名某一个在那种适配器上就是一句假话。
    """
    if required and not paths:
        raise RuntimeError(
            f"测试未产出任何 JUnit 报告（worktree={worktree}）："
            "测试进程没能正常跑完（超时被杀 / 崩溃 / 沙箱执行失败），"
            "本次结果不可信")


async def _rm_reports(sb: LocalSandbox, adapter: PytestAdapter,
                      worktree: Path, scoped: bool) -> None:
    """删掉本次跑出的报告 —— 可能不止一份。

    不留产物是硬要求：Worktree.commit() 的 git add -A 会把它扫进交付分支。
    """
    stale = adapter.report_paths(worktree, scoped=scoped)
    if stale:
        await sb.exec(["rm", "-f", *(str(p) for p in stale)], 10.0)


async def run_full_suite(worktree: Path, adapter: PytestAdapter,
                         timeout: float = 900.0,
                         require_report: bool = False):
    """在 worktree 里跑全量测试并解析报告。零 LLM。

    require_report 默认 False：核心循环容忍报告缺失（见 _check_report）。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.full_test_command(), timeout)
        paths = adapter.report_paths(worktree)
        _check_report(worktree, paths, require_report)
        return parse_junit(paths, adapter.make_test_id)
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=False)
        await sb.close()


async def run_scoped(worktree: Path, adapter: PytestAdapter,
                     test_ids: list[str], timeout: float = 300.0,
                     require_report: bool = False):
    """只跑指定用例并解析报告。供 flaky 确认使用 —— 成本远低于全量。

    走 scoped 那份报告：调用它的时候全量那份通常还要继续用，不能被覆盖。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.scoped_test_command(test_ids), timeout)
        paths = adapter.report_paths(worktree, scoped=True)
        _check_report(worktree, paths, require_report)
        return parse_junit(paths, adapter.make_test_id)
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=True)
        await sb.close()


async def baseline_node(state: AifixState) -> dict[str, Any]:
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。"""
    adapter = adapter_for(state["adapter_name"])
    fs = await run_full_suite(Path(state["worktree_path"]), adapter)
    ids = sorted(fs.ids)
    return {"baseline_ids": ids, "queue": list(ids),
            "_failures": dict(fs.failures)}
