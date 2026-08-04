import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.eval.mine import mine_tasks
from aifix.eval.runner import run_suite
from aifix.eval.score import render_table, summarize
from aifix.eval.task import Task, TaskResult, read_jsonl, write_jsonl

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


async def test_mine_then_eval_then_table(history_repo, tmp_path):
    """挖 → 跑 → 打分 → 出表，全链路一次跑通。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")
    assert len(tasks) == 1

    tasks_file = tmp_path / "evals" / "tasks.jsonl"
    write_jsonl(tasks_file, tasks)
    assert read_jsonl(tasks_file, Task) == tasks

    results = await run_suite(
        read_jsonl(tasks_file, Task), AifixConfig(), "假模型",
        tmp_path / "w", parallel=2,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))

    results_file = tmp_path / "evals" / "results.jsonl"
    write_jsonl(results_file, results)
    back = read_jsonl(results_file, TaskResult)

    s = summarize(back)
    assert s.tasks == 1
    assert s.fix_rate == 1.0
    assert s.locate_rate == 1.0
    assert s.errors == 0

    md = render_table([s])
    assert "假模型" in md
    assert "100%" in md


async def test_two_labels_render_side_by_side(history_repo, tmp_path):
    """跨模型对比的形状：同一批任务，两轮结果并排。"""
    tasks = await mine_tasks(str(history_repo["path"]), PytestAdapter(),
                             limit=10, workdir=tmp_path / "m")

    good = await run_suite(
        tasks, AifixConfig(), "会修的", tmp_path / "a", parallel=1,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    lazy = await run_suite(
        tasks, AifixConfig(), "不修的", tmp_path / "b", parallel=1,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_text("我觉得没问题")]))

    sg, sl = summarize(good), summarize(lazy)
    assert sg.fix_rate == 1.0
    assert sl.fix_rate == 0.0
    # 两边定位都对：差别只在改没改。这正是双档分开的意义 ——
    # 合成一个数字就看不出「Detector 没问题，是 Fixer 不干活」。
    assert sg.locate_rate == sl.locate_rate == 1.0

    md = render_table([sg, sl])
    rows = [ln for ln in md.splitlines() if ln.startswith("| 会修的")
            or ln.startswith("| 不修的")]
    assert len(rows) == 2
    # 注意：不能写 `"0%" in row` —— 它是 "100%" 的子串，那样的断言恒为真。
    def _cells(r):
        return [c.strip() for c in r.strip("|").split("|")]
    # 按**列名**取列，不按下标。M4 里这张表插过两次新列（「来源」「可疑信号」），
    # 每插一次都让写死下标的断言错位一次 —— 而第二次错位**照样是绿的**：下标 3
    # 从「修复成功率」滑到了「定位准确率」，那一列两行都是 100%，恰好把「不修的」
    # 那行本该断言 0% 的事实盖住了。按列名取，以后插列不必再改这里。
    header = _cells(next(ln for ln in md.splitlines()
                         if ln.startswith("| 模型")))
    col = next(i for i, h in enumerate(header) if h.startswith("修复成功率"))
    # 整格相等，连样本量与区间一起钉住：1/1 的 100% 和 12/20 的 60% 不该长得
    # 一样，这正是给这两列加分数与 Wilson 区间的理由。
    assert _cells(rows[0])[col] == "100% (1/1, 95%CI 21%–100%)", rows[0]
    assert _cells(rows[1])[col] == "0% (0/1, 95%CI 0%–79%)", rows[1]
