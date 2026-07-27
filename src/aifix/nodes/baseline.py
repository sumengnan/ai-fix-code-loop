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


async def run_full_suite(worktree: Path, adapter: PytestAdapter,
                         timeout: float = 900.0):
    """在 worktree 里跑全量测试并解析报告。零 LLM。"""
    report = adapter.report_glob()
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.full_test_command(report), timeout)
        return parse_junit([worktree / report], adapter.make_test_id)
    finally:
        await sb.exec(["rm", "-f", report], 10.0)
        await sb.close()


async def baseline_node(state: AifixState) -> dict[str, Any]:
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。"""
    adapter = adapter_for(state["adapter_name"])
    fs = await run_full_suite(Path(state["worktree_path"]), adapter)
    ids = sorted(fs.ids)
    return {"baseline_ids": ids, "queue": list(ids),
            "_failures": dict(fs.failures)}
