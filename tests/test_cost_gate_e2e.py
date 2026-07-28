"""成本闸的端到端：从配置到报告，钱真的被拦住了。"""
import json

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

# 价表的键必须是**模型名**，而 HarnessConfig().model 默认是 "gpt-4o-mini"
# （不是空串 —— 写错的话 effective_cost 匹配不到价表、成本恒为 0，
# 成本闸的测试会全部沦为空转而依然「通过」）。
# 单价定得极高：effective_cost = 输入/1k×价 + 输出/1k×价，
# Usage(10,5,15) 配 [1000,1000] 恰好是 10+5=15 美元，一次调用就越线。
_PRICEY = {"gpt-4o-mini": [1000.0, 1000.0]}


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
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


async def test_usd_budget_stops_the_run(buggy_repo):
    """极小的美元额度 —— fix 不该跑满，报告要如实说没修好。"""
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")])
    state = await run_once(
        buggy_repo, AifixConfig(budget_usd=0.0001, price_map=_PRICEY),
        run_id="cap1",
        detector_client=_Scripted([_text(_DIAG)]), fixer_client=fixer)
    assert state["results"][0]["verdict"] != "better"
    assert fixer.calls <= 1, "越线后不该再发起新的模型调用"


async def test_generous_budget_lets_it_finish(buggy_repo):
    """对照组：额度足够时行为不变，证明上面那条测的是闸不是别的。"""
    state = await run_once(
        buggy_repo, AifixConfig(budget_usd=1000.0, price_map=_PRICEY),
        run_id="cap2",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    assert state["results"][0]["verdict"] == "better"


async def test_budget_abort_still_records_in_flight_failure(buggy_repo):
    """预算中止时，手上正在处理的 failure 不能从 results 里彻底消失。

    attempt 1 没修好只会把 attempt 递增为下一轮编号、不落记录；如果
    下一轮开头就因预算耗尽 break，这个 failure 就该被主循环补录，而
    不是不留痕迹——否则报告会显示「修复 0/1」却配一张空表，用户
    没法区分「试过没修好」与「压根没轮到」。
    """
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")])
    state = await run_once(
        buggy_repo, AifixConfig(budget_usd=0.0001, price_map=_PRICEY),
        run_id="cap3",
        detector_client=_Scripted([_text(_DIAG)]), fixer_client=fixer)
    rows = [r for r in state["results"]
            if r["test_id"] == "tests/test_calc.py::test_add"]
    assert len(rows) == 1, "越线的 failure 必须在 results 里留下恰好一行"
    row = rows[0]
    assert row["abort_reason"] not in (None, "max_attempts"), (
        "中止原因必须是预算耗尽的说明，不能是 max_attempts —— "
        "它根本没跑到重试上限，是被钱掐断的")
    assert "预算" in row["abort_reason"]
    assert row["attempts"] >= 1


async def test_guard_giveup_shows_in_report(buggy_repo):
    """连续空 diff 放弃后，报告里的中止原因要能区分出是哪一条守卫。"""
    state = await run_once(
        buggy_repo, AifixConfig(fix_guard_retries=5, guard_giveup_limit=2),
        run_id="giveup1",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_text("已修复")]))
    assert "empty_diff_giveup" in state["report_md"]
