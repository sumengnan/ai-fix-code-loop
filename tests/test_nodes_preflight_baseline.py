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

    def full_test_command(self) -> list[str]:
        return [sys.executable, "-c", ""]

    def scoped_test_command(self, test_ids) -> list[str]:
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


async def test_missing_report_message_names_no_particular_file(buggy_repo):
    """报告可以有多份（Maven surefire 每个测试类一份）。

    消息里点名某一个文件，在多报告适配器上就是一句假话 —— 本项目把
    「消息说了一件代码没做的事」与「数字造假」同等对待。
    """
    with pytest.raises(RuntimeError) as ei:
        await run_full_suite(buggy_repo, _SilentAdapter(), require_report=True)
    msg = str(ei.value)
    assert ".xml" not in msg, f"消息里点名了具体报告文件：{msg}"
    assert str(buggy_repo) in msg, f"消息没说是哪个 worktree：{msg}"


async def test_run_full_suite_result_is_unchanged_by_the_refactor(buggy_repo):
    """行为不变基准：接口从「一个路径」改成「一组路径」前实测的失败集。

    基准值由改造前的代码在同一个 buggy_repo 夹具上真跑一次得到，逐点写死。
    接口重构最典型的失败形状是「测试全绿，但某条路径悄悄少解析了一份报告」——
    只断言 ids 非空是发现不了的，所以连 ran / 字段值一起钉住。
    """
    fs = await run_full_suite(buggy_repo, PytestAdapter())
    assert fs.ids == {"tests/test_calc.py::test_add"}
    assert set(fs.ran) == {"tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"}
    f = fs.failures["tests/test_calc.py::test_add"]
    assert f.classname == "tests.test_calc"
    assert f.name == "test_add"
    assert f.file == "tests/test_calc.py"
    assert f.line == 3
    assert f.message == "assert -1 == 5\n +  where -1 = add(2, 3)"
    assert "add(2, 3)" in f.trace
    # 跑完不留产物：Worktree.commit() 的 git add -A 会把它扫进交付分支
    assert list(buggy_repo.glob(".aifix-*.xml")) == []


async def test_run_scoped_result_is_unchanged_by_the_refactor(buggy_repo):
    """同上，scoped 那条路径的基准。两个用例都点名跑，只有 test_add 该红。"""
    fs = await run_scoped(buggy_repo, PytestAdapter(),
                          ["tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"])
    assert fs.ids == {"tests/test_calc.py::test_add"}
    assert set(fs.ran) == {"tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"}
    assert list(buggy_repo.glob(".aifix-*.xml")) == []


async def test_scoped_run_does_not_clobber_the_full_report(buggy_repo):
    """复跑写的是另一份报告 —— 否则全量那份被覆盖后又被清理删掉。

    flaky 复跑就发生在全量结果还要继续用的时候，覆盖等于把 baseline 换成
    一份只含两三个用例的报告。
    """
    a = PytestAdapter()
    sentinel = "<testsuites/>"
    (buggy_repo / a.REPORT_NAME).write_text(sentinel, encoding="utf-8")
    try:
        await run_scoped(buggy_repo, a, ["tests/test_calc.py::test_identity"])
        assert (buggy_repo / a.REPORT_NAME).read_text(encoding="utf-8") == sentinel
    finally:
        (buggy_repo / a.REPORT_NAME).unlink(missing_ok=True)


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
