"""端到端验收：红色测试进去，绿色分支出来，主工作区未被触碰。

用脚本化模型替身，不打网络。
"""
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
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


async def test_red_in_green_out(buggy_repo):
    detector = _Scripted([_text(_DIAG)])
    fixer = _Scripted([
        _tool("apply_patch", json.dumps({"diff": _PATCH})),
        _text("已修复"),
    ])
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")

    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e",
                           detector_client=detector, fixer_client=fixer)

    # 1. 目标用例被判定为修复
    assert [r["verdict"] for r in state["results"]] == ["better"]
    # 2. 主工作区一字未动
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original
    # 3. 修复落在独立分支上
    show = subprocess.run(["git", "show", "aifix/e2e:calc.py"],
                          cwd=buggy_repo, capture_output=True, text=True)
    assert "a + b" in show.stdout
    # 4. 报告可读
    assert "1 / 1" in state["report_md"]


async def test_green_repo_reports_no_work(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e2",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["results"] == []
    assert "0 / 0" in state["report_md"]


async def test_dirty_repo_aborts(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e3",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["abort"] is not None
    assert "工作区不干净" in state["report_md"]
