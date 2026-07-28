import sys

import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import AifixState, new_state
from aifix.nodes.baseline import baseline_node, run_full_suite, run_scoped
from aifix.nodes.preflight import preflight_node


def test_new_state_defaults(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    assert st["run_id"] == "r1"
    assert st["queue"] == []
    assert st["current"] is None
    assert st["attempt"] == 0
    assert st["results"] == []


def test_preflight_detects_adapter_and_rejects_dirty(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["adapter_name"] == "pytest"
    assert out["abort"] is None

    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    st2 = new_state(buggy_repo, AifixConfig(), run_id="r2")
    out2 = preflight_node(st2)
    assert out2["abort"] is not None
    assert "工作区不干净" in out2["abort"]


def test_preflight_rejects_unknown_project(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    st = new_state(tmp_path, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["abort"] is not None
    assert "适配器" in out["abort"]


async def test_baseline_collects_failures(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert "tests/test_calc.py::test_add" in out["baseline_ids"]
    assert "tests/test_calc.py::test_identity" not in out["baseline_ids"]
    assert out["queue"] == ["tests/test_calc.py::test_add"]
    assert "tests/test_calc.py::test_add" in out["_failures"]


class _SilentAdapter(PytestAdapter):
    """跑一个什么都不做的命令：模拟测试进程被超时杀掉、根本没写出报告。"""

    def full_test_command(self, report_path: str) -> list[str]:
        return [sys.executable, "-c", ""]

    def scoped_test_command(self, test_ids, report_path: str) -> list[str]:
        return [sys.executable, "-c", ""]


async def test_missing_report_is_tolerated_by_default(buggy_repo):
    """M1/M2 的既有行为：报告缺失 → 空集合，不抛 —— 不能改。"""
    fs = await run_full_suite(buggy_repo, _SilentAdapter())
    assert fs.ids == set()
    fs2 = await run_scoped(buggy_repo, _SilentAdapter(), ["随便一个"])
    assert fs2.ids == set()


async def test_missing_report_raises_when_required(buggy_repo):
    """挖任务时「没跑成」必须与「跑完了、全绿」区分开。

    否则空集合会被当成全绿，`red - green` 把 base 处所有红的用例
    全部当成「红转绿」吐出来 —— 凭空捏造一整批任务。
    """
    with pytest.raises(RuntimeError, match="报告"):
        await run_full_suite(buggy_repo, _SilentAdapter(), require_report=True)
    with pytest.raises(RuntimeError, match="报告"):
        await run_scoped(buggy_repo, _SilentAdapter(), ["随便一个"],
                         require_report=True)


async def test_baseline_on_green_repo_yields_empty_queue(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    import subprocess
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert out["queue"] == []
