import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure, Verdict
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.detect import detect_node
from aifix.nodes.fix import fix_node
from aifix.nodes.preflight import preflight_node
from aifix.nodes.verify import verify_node

_TID = "tests/test_calc.py::test_add"

_DIAG_JSON = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""


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


def _tool(name, args, cid="c1"):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id=cid, name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _failure_state(buggy_repo, wt):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["baseline_ids"] = [_TID]
    st["queue"] = []
    st["current"] = _TID
    st["attempt"] = 1
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="tests.test_calc", name="test_add",
        message="assert -1 == 5", trace="E assert -1 == 5")}
    return st


async def test_detect_produces_diagnosis(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        out = await detect_node(st, client=_Scripted([_text(_DIAG_JSON)]))
        assert out["diagnosis"]["suspect_file"] == "calc.py"
        assert out["spent_tokens"] == 15


async def test_detect_degrades_on_garbage(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        out = await detect_node(st, client=_Scripted([_text("不是 JSON")]))
        assert out["diagnosis"] is None      # 降级而非中止


async def test_fix_applies_patch(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        st["diagnosis"] = json.loads(_DIAG_JSON)
        client = _Scripted([
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
            _text("已修复"),
        ])
        out = await fix_node(st, client=client)
        assert out["abort"] is None
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_better_commits(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        out = await verify_node(st)
        assert out["verdict"] == Verdict.BETTER.value
        assert out["current"] is None            # 该 failure 完结
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_same_rolls_back(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(
            "def add(a, b):\n    return a - b  # 无用改动\n", encoding="utf-8")
        out = await verify_node(st)
        assert out["verdict"] == Verdict.SAME.value
        assert "无用改动" not in (wt.path / "calc.py").read_text(encoding="utf-8")


async def test_verify_gives_up_after_max_attempts(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _failure_state(buggy_repo, wt)
        st["attempt"] = st["config"].max_attempts
        out = await verify_node(st)
        assert out["current"] is None
        assert out["results"][-1]["abort_reason"] == "max_attempts"
