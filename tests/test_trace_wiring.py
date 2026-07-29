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


async def test_import_anchored_detect_is_not_recorded_as_a_guess(
        buggy_repo, tmp_path):
    """import 推出来的候选也是锚点——但要和栈帧锚点分得开。

    `tests/test_calc.py` 写着 `from calc import add`，所以纯断言失败下
    Detector 不再是无锚猜测，suspect_unanchored 不该再写。但这两种锚点
    强度不同（栈帧是「失败真的穿过这里」，import 只是「测试用到了它」），
    trace 里必须留得下这个区别，否则跨 run 统计分不清定位是靠什么来的，
    也没法回答「退到 import 之后定位准确率动没动」。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        # 纯断言失败：栈上只有测试文件那一帧
        st = _state(buggy_repo, wt, trace,
                    "tests/test_calc.py:5: AssertionError")
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    facts = _facts(tmp_path)
    keys = {f["key"] for f in facts}
    assert "suspect_unanchored" not in keys, "有 import 锚点就不是盲猜"
    kind = [f for f in facts if f["key"] == "suspect_anchor"]
    assert kind and kind[0]["value"] == "import", facts


async def test_traceback_anchor_is_labelled_as_such(buggy_repo, tmp_path):
    """有源码栈帧时锚点种类是 traceback —— 与 import 那条互为对照。

    只断言 import 那一条的话，一个恒返回 "import" 的实现也能通过。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        trace = RunTrace(tmp_path, run_id="r1")
        st = _state(buggy_repo, wt, trace,
                    f'File "{wt.path}/calc.py", line 2, in add\n')
        await detect_node(st, client=_Scripted([_text(_DIAG)]))
        trace.close()

    kind = [f for f in _facts(tmp_path) if f["key"] == "suspect_anchor"]
    assert kind and kind[0]["value"] == "traceback"


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


async def test_events_carry_failure_and_attempt(buggy_repo):
    """事件流必须自带归属，否则回放只能猜「这一步是修哪个用例的第几次尝试」。

    猜出来的时间轴看着精确、读的人会据此下判断，实际是错位的 —— 归属只能
    由写事件的那一侧带上，事后对不回来。
    """
    from harness.llm.base import ToolCallDelta

    from aifix.cli import run_once

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

    await run_once(buggy_repo, AifixConfig(), run_id="attr",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch", json.dumps({"diff": patch})),
                       _text("已修复")]))
    d = buggy_repo / ".aifix" / "runs" / "attr"

    events = [json.loads(x) for x in
              (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events, "前提没成立：这次 run 没落下事件"
    # 这一轮只有一个 failure、一次 attempt：所有事件的归属都必须是它，
    # 而且必须**每一条**都有 —— 少数几条带上不足以拼时间轴
    assert {(e.get("failure"), e.get("attempt")) for e in events} == \
        {("tests/test_calc.py::test_add", 1)}

    # 与同一轮的 facts 对得上：两份文件各写各的归属，值不一致等于没有归属
    facts = _facts(d)
    verdict = [f for f in facts if f["key"] == "verdict"]
    assert verdict, "前提没成立：这次 run 没落下 verdict"
    assert (verdict[0]["failure"], verdict[0]["attempt"]) == \
        (events[0]["failure"], events[0]["attempt"])


async def test_run_level_facts_have_no_attribution(buggy_repo):
    """对照组：run 级的事实不该被塞上 failure / attempt。

    没有这一条，一个「所有记录都写死同一个 failure」的实现也能让上面全绿。
    """
    from aifix.cli import run_once

    await run_once(buggy_repo, AifixConfig(), run_id="attr2", dry_run=True,
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([_text("x")]))
    facts = _facts(buggy_repo / ".aifix" / "runs" / "attr2")
    baseline = [f for f in facts if f["key"] == "baseline_failures"]
    assert baseline and "failure" not in baseline[0]
    assert "attempt" not in baseline[0]


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
