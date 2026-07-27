import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import AifixState, new_state
from aifix.nodes.baseline import baseline_node
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
