import json

from harness.llm.base import StreamChunk
from harness.usage import Usage

from aifix.adapters.base import Failure
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.detect import detect_node
from aifix.nodes.preflight import preflight_node
from aifix.trace import RunTrace

_TID = "tests/test_calc.py::test_add"
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "high"})


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _facts(d):
    return [json.loads(x) for x in
            (d / "facts.jsonl").read_text(encoding="utf-8").splitlines()]


def _state(buggy_repo, wt, trace_obj, trace_text):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["current"] = _TID
    st["attempt"] = 1
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="c", name="n", message="m", trace=trace_text)}
    if trace_obj is not None:
        st["_trace"] = trace_obj
    return st


async def test_detect_records_suspect_in_traceback(buggy_repo, tmp_path):
    """suspect_file 是否落在 traceback 指出的文件里。

    注意这**不是**规格 §9 的 locate_hit（那个对 ground truth 判定，
    由评测计算）。两者是不同的集合：traceback 指向的文件未必是该改的
    文件 —— 异常常在下游抛出，缺陷却在上游。共用一个名字会让评测
    悄悄量错东西，所以在这里就分开。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace,
                    f'File "{wt.path}/calc.py", line 2, in add\n')
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    hit = [f for f in _facts(tmp_path) if f["key"] == "suspect_in_traceback"]
    assert hit and hit[0]["value"] is True


async def test_detect_records_traceback_miss(buggy_repo, tmp_path):
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    hit = [f for f in _facts(tmp_path) if f["key"] == "suspect_in_traceback"]
    assert hit and hit[0]["value"] is False


async def test_detect_records_parse_failure(buggy_repo, tmp_path):
    """模型没吐出合法 JSON 时也要留痕，否则 suspect_in_traceback 的分母不可信。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        out = await detect_node(st, client=_Scripted([_text("不是 JSON")]))
        trace.close()
        assert out["diagnosis"] is None

    keys = {f["key"] for f in _facts(tmp_path)}
    assert "diagnosis_parse_failed" in keys


async def test_detect_writes_raw_events(buggy_repo, tmp_path):
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    types = {json.loads(x)["type"] for x in
             (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()}
    assert "RunStarted" in types


async def test_fix_records_every_guard_retry_round(buggy_repo, tmp_path):
    """守卫重试时，「一字未改」那一轮的事件也必须落盘 —— 它最该复盘。"""
    from harness.llm.base import ToolCallDelta

    from aifix.nodes.fix import fix_node

    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

    def _tool(name, args):
        return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=args)),
                StreamChunk(type="done", usage=Usage(10, 5, 15))]

    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        st["baseline_ids"] = [_TID]
        client = _Scripted([_text("已修复"),
                            _tool("apply_patch", json.dumps({"diff": patch})),
                            _text("这次真改了")])
        out = await fix_node(st, client=client)
        trace.close()
        assert out["guard_hits"] == ["empty_diff"]

    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    starts = [e for e in events if json.loads(e)["type"] == "RunStarted"]
    assert len(starts) == 2, "两轮 AgentLoop 都要留痕，不能只记最后一轮"

    keys = [f["key"] for f in _facts(tmp_path)]
    assert "guard_hit" in keys


async def test_detect_without_trace_still_works(buggy_repo):
    """trace 缺席不该影响主流程（单测直接调节点时没有 trace）。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, None, "")
        out = await detect_node(st, client=_Scripted([_text(_DIAG)]))
        assert out["diagnosis"]["suspect_file"] == "calc.py"


async def test_fix_records_violations(buggy_repo, tmp_path):
    """模型想改测试文件 —— 被工具挡下，且必须留下可统计的痕迹。"""
    from harness.llm.base import ToolCallDelta

    from aifix.nodes.fix import fix_node

    bad = """--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -3,4 +3,4 @@
 def test_add():
-    assert add(2, 3) == 5
+    assert True
"""
    good = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

    def _tool(name, args):
        return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=args)),
                StreamChunk(type="done", usage=Usage(10, 5, 15))]

    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        st["baseline_ids"] = [_TID]
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": bad})),
                            _tool("apply_patch", json.dumps({"diff": good})),
                            _text("改源码了")])
        await fix_node(st, client=client)
        trace.close()

    facts = _facts(tmp_path)
    viol = [f for f in facts if f["key"] == "violation"]
    assert [v["value"] for v in viol] == ["test_edit"]


async def test_fix_records_no_violation_when_clean(buggy_repo, tmp_path):
    from harness.llm.base import ToolCallDelta

    from aifix.nodes.fix import fix_node

    good = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

    def _tool(name, args):
        return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=args)),
                StreamChunk(type="done", usage=Usage(10, 5, 15))]

    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace, "")
        st["baseline_ids"] = [_TID]
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": good})),
                            _text("已修复")])
        await fix_node(st, client=client)
        trace.close()

    assert [f for f in _facts(tmp_path) if f["key"] == "violation"] == []
