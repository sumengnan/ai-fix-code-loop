import json
import subprocess

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
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


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _fixer():
    return _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                      _text("已修复")])


async def test_artifacts_written(buggy_repo):
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="art1",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_fixer())
    d = buggy_repo / ".aifix" / "runs" / "art1"
    assert (d / "report.md").is_file()
    assert (d / "facts.jsonl").is_file()
    assert (d / "events.jsonl").is_file()
    assert "1 / 1" in (d / "report.md").read_text(encoding="utf-8")
    assert state["results"][0]["verdict"] == "better"


async def test_facts_contain_verdict_and_locate_hit(buggy_repo):
    await run_once(
        buggy_repo, AifixConfig(), run_id="art2",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_fixer())
    d = buggy_repo / ".aifix" / "runs" / "art2"
    keys = {json.loads(x)["key"] for x in
            (d / "facts.jsonl").read_text(encoding="utf-8").splitlines()}
    assert "verdict" in keys
    assert "locate_hit" in keys
    assert "diff_lines" in keys
    assert "baseline_failures" in keys


async def test_facts_carry_span_coordinates(buggy_repo):
    """每条 verdict 都要能归属到具体 failure 与 attempt。"""
    await run_once(
        buggy_repo, AifixConfig(), run_id="art4",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_fixer())
    d = buggy_repo / ".aifix" / "runs" / "art4"
    facts = [json.loads(x) for x in
             (d / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    verdicts = [f for f in facts if f["key"] == "verdict"]
    assert verdicts
    assert verdicts[0]["failure"] == "tests/test_calc.py::test_add"
    assert verdicts[0]["attempt"] == 1


class _Explodes:
    """任何模型调用都算失败 —— 用来证明某条路径确实零 LLM。"""

    async def stream(self, messages, tools):
        raise AssertionError("这条路径不该调用模型")
        yield


async def test_dry_run_calls_no_model(buggy_repo):
    """--dry-run 只报告有多少活，不烧一分钱。"""
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="dry1", dry_run=True,
        detector_client=_Explodes(), fixer_client=_Explodes())
    assert state["baseline_ids"] == ["tests/test_calc.py::test_add"]
    assert state["results"] == []
    assert state["spent_tokens"] == 0
    keys = {json.loads(x)["key"] for x in
            (buggy_repo / ".aifix" / "runs" / "dry1" / "facts.jsonl")
            .read_text(encoding="utf-8").splitlines()}
    assert "dry_run" in keys


async def test_only_test_filters_queue(buggy_repo):
    """--test 指定一个不存在的用例时，队列应为空而非全跑。"""
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="only1",
        only_test="tests/test_calc.py::不存在",
        detector_client=_Explodes(), fixer_client=_Explodes())
    assert state["results"] == []


async def test_only_test_keeps_matching_failure(buggy_repo):
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="only2",
        only_test="tests/test_calc.py::test_add",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_fixer())
    assert [r["test_id"] for r in state["results"]] == [
        "tests/test_calc.py::test_add"]


async def test_delivery_branch_has_only_source(buggy_repo):
    """交付分支不该有构建产物 —— 任务 1-3 的最终验收。"""
    await run_once(
        buggy_repo, AifixConfig(), run_id="art3",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_fixer())
    files = subprocess.run(
        ["git", "diff", "--name-only", "main..aifix/art3"],
        cwd=buggy_repo, capture_output=True, text=True).stdout.split()
    assert files == ["calc.py"], f"交付分支混入了非源码文件：{files}"
