import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.config import AifixConfig
from aifix.eval.runner import first_attempt_suspect, run_suite, run_task
from aifix.eval.task import Task

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
_WRONG_DIAG = json.dumps({
    "suspect_file": "别的文件.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "low"})


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


def _task(h) -> Task:
    return Task(task_id="hist@t1::test_add", repo=str(h["path"]),
                commit=h["commit"], base_commit=h["base"],
                test_files=h["test_files"], target_test=h["target"],
                gold_files=h["gold_files"])


async def test_successful_task_scores_better_and_locate_hit(
        history_repo, tmp_path):
    r = await run_task(_task(history_repo), AifixConfig(), "假模型",
                       tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.verdict == "better"
    assert r.locate_hit is True           # calc.py ∈ gold_files
    assert r.suspect_file == "calc.py"
    assert r.model == "假模型"
    assert r.error is None
    assert r.tokens > 0


async def test_locate_miss_when_suspect_not_in_gold(history_repo, tmp_path):
    """定位准确率必须对 ground truth 判，不能对 traceback 判。"""
    r = await run_task(_task(history_repo), AifixConfig(), "假模型",
                       tmp_path / "w",
                       detector_client=_Scripted([_text(_WRONG_DIAG)]),
                       fixer_client=_fixer())
    assert r.locate_hit is False
    assert r.suspect_file == "别的文件.py"
    assert r.verdict == "better", "定位错了但改对了 —— 两档分开算"


async def test_baseline_not_reproduced_is_an_error_not_a_failure(
        history_repo, tmp_path):
    """任务本身失效要与「没修好」分开，否则会污染成功率。"""
    t = _task(history_repo).model_copy(
        update={"target_test": "tests/test_calc.py::根本不存在"})
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.error is not None
    assert "复现" in r.error


def test_suspect_is_taken_from_the_first_attempt():
    facts = [{"key": "suspect_file", "value": "calc.py", "attempt": 1},
             {"key": "suspect_file", "value": "别的.py", "attempt": 2}]
    assert first_attempt_suspect(facts) == "calc.py"


def test_parse_failure_in_first_attempt_does_not_slide_to_the_second():
    """定位准确率量的是 Detector 的冷启动能力，不能滑到第 2 轮。

    第 2/3 轮已经看过失败反馈，是一道更容易的题；而 detect_node 在 JSON
    解析失败时压根不写 suspect_file，于是「取第一条 suspect_file」会
    系统性地把第 2 轮的诊断算进去 —— 抬高幅度正比于模型的 JSON 合规性
    有多差，恰好是跨模型对比里最不该被混淆的维度。
    """
    facts = [{"key": "diagnosis_parse_failed", "value": True, "attempt": 1},
             {"key": "suspect_file", "value": "calc.py", "attempt": 2}]
    assert first_attempt_suspect(facts) is None


def test_facts_without_attempt_are_ignored():
    """attempt 坐标缺失说明这条事实不是在 attempt_span 里记的，不可信。"""
    assert first_attempt_suspect(
        [{"key": "suspect_file", "value": "calc.py"}]) is None


def _fake_run_once(monkeypatch, target: str, **over):
    """把 run_once 换成一个只返回既定 state 的桩。

    预算中止发生在「attempt 1 没修好、还没进 attempt 2」的空档，真实闭环
    要烧掉预算才能复现 —— 桩掉 run_once 是唯一划算的构造方式。
    """
    import aifix.eval.runner as runner

    state = {"baseline_ids": [target], "results": [], "attempt": 2,
             "spent_tokens": 1234, "spent_usd": 0.0, "abort": None,
             "abort_kind": None}
    state.update(over)

    async def fake(*a, **kw):
        return state

    monkeypatch.setattr(runner, "run_once", fake)
    return state


async def test_budget_abort_keeps_the_attempts_actually_made(
        history_repo, tmp_path, monkeypatch):
    """预算中止时任务明明真跑过一轮，attempts 不能记成 0。

    verify_node 只在 verdict=better 或 attempt≥max_attempts 时才写
    results；attempt 1 没修好、随后预算耗尽 break，results 仍是空的 ——
    落成 0 会把「平均尝试」系统性拉低。
    """
    t = _task(history_repo)
    _fake_run_once(monkeypatch, t.target_test,
                   abort="token 预算耗尽：50000 / 50000", abort_kind="tokens")
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w")
    assert r.attempts == 1
    assert r.verdict == "same"
    assert r.error is None, "token 预算是模型的属性，没在预算内修好是真实成绩"


async def test_suite_isolates_failures(history_repo, tmp_path):
    """一个任务炸掉不能带走整个 suite。"""
    ok = _task(history_repo)
    bad = ok.model_copy(update={"task_id": "坏的", "repo": "/不存在的路径"})
    rs = await run_suite([ok, bad], AifixConfig(), "假模型", tmp_path / "w",
                         parallel=2,
                         detector_client=_Scripted([_text(_DIAG)]),
                         fixer_client=_fixer())
    by_id = {r.task_id: r for r in rs}
    assert by_id["坏的"].error is not None
    assert by_id[ok.task_id].verdict == "better"


async def test_suite_preserves_order(history_repo, tmp_path):
    ts = [_task(history_repo).model_copy(update={"task_id": f"t{i}"})
          for i in range(3)]
    rs = await run_suite(ts, AifixConfig(), "假模型", tmp_path / "w",
                         parallel=2,
                         detector_client=_Scripted([_text(_DIAG)]),
                         fixer_client=_fixer())
    assert [r.task_id for r in rs] == ["t0", "t1", "t2"]
