import asyncio
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.config import AifixConfig
from aifix.eval.runner import (first_attempt_suspect, locate_hit, run_suite,
                               run_task)
from aifix.eval.task import Task, TaskResult

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


def _task(h, origin: str = "mined") -> Task:
    return Task(task_id="hist@t1::test_add", repo=str(h["path"]),
                commit=h["commit"], base_commit=h["base"],
                test_files=h["test_files"], target_test=h["target"],
                gold_files=h["gold_files"], origin=origin)


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


async def test_task_result_counts_signals_from_facts(
        history_repo, tmp_path, monkeypatch):
    """信号条数从 facts.jsonl 数出来 —— 和 violations 同一条路径。

    files_outside_suspect 这条 fact 的 value 是列表（这里放了 3 个文件），
    若按列表长度记就会把总数从 3 撑到 5——「改了一堆文件」这一种情形会把
    这一列冲爆，掩盖掉「到底出现了几类信号」。所以必须按 fact 条数记：
    三类信号各出现一次，无论 files_outside_suspect 里有多少个文件，都只
    算一条。
    """
    import aifix.eval.runner as R

    facts = [
        {"key": "suspect_file", "value": "calc.py", "attempt": 1},
        {"key": "removed_public_symbol", "value": "mul", "attempt": 1},
        {"key": "new_module_state", "value": "_CACHE", "attempt": 1},
        {"key": "files_outside_suspect", "value": ["a.py", "b.py", "c.py"],
         "attempt": 1},
    ]
    monkeypatch.setattr(R, "_read_facts", lambda *a, **kw: facts)
    r = await run_task(_task(history_repo), AifixConfig(), "假模型",
                       tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.signals == 3


async def test_run_task_result_carries_task_origin(history_repo, tmp_path):
    """origin 要跟着 task 走，不能被写死成 mined —— 否则变异任务的统计
    会被并进挖掘任务里，抵消掉「按来源分行」的意义。"""
    t = _task(history_repo, origin="mutated")
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.origin == "mutated"


async def test_baseline_not_reproduced_keeps_task_origin(
        history_repo, tmp_path):
    """baseline 未复现那条 blank 结果也要带上 origin —— 这条路径用的是
    run_task 里的局部 blank，不是模块级 _blank，同样不能漏。"""
    t = _task(history_repo, origin="mutated").model_copy(
        update={"target_test": "tests/test_calc.py::根本不存在"})
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w",
                       detector_client=_Scripted([_text(_DIAG)]),
                       fixer_client=_fixer())
    assert r.error is not None
    assert r.origin == "mutated"


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


async def test_wall_clock_abort_is_an_eval_fault_not_a_model_failure(
        history_repo, tmp_path, monkeypatch):
    """墙钟预算是评测调度器的属性，不是模型的属性。

    --parallel 8 时八个任务在同一台机器上抢 CPU 跑全量 pytest，墙钟耗尽的
    概率远高于 --parallel 1 —— 记成模型的失败，就等于「只改并行度就能改变
    修复成功率」，直接违背跨模型对比的前提。
    """
    t = _task(history_repo)
    _fake_run_once(monkeypatch, t.target_test,
                   abort="时间预算耗尽：1800s / 1800s", abort_kind="wall")
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w")
    assert r.error is not None
    assert "时间" in r.error or "墙钟" in r.error


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


# locate_hit：M3 跨模型评测第一次真跑，deepseek-v4-pro 与 deepseek-v4-flash
# 都把定位准确率判成了 0% —— 两个模型给出的 suspect_file 是模块路径形式
# （`aifix/eval/mine.py`），gold_files 是仓库路径形式（`src/aifix/eval/
# mine.py`），两者其实指向同一个文件，裸字符串相等的旧判定却算作没命中。
# 下面这组测试锁定「路径分段后缀匹配」的语义，防止再退化回裸字符串比较。

def test_locate_hit_module_path_vs_repo_path():
    """模型写模块路径（少一段 src/ 前缀），gold 是仓库路径——应命中。"""
    assert locate_hit("aifix/eval/mine.py", ["src/aifix/eval/mine.py"])


def test_locate_hit_bare_filename_counts_as_located():
    """模型只报文件名——分段序列长度为 1，仍是 gold 路径的后缀，应命中。"""
    assert locate_hit("mine.py", ["src/aifix/eval/mine.py"])


def test_locate_hit_is_symmetric():
    """suspect 比 gold 更长时也要能命中，不能假设 gold 总是更长的那一边。"""
    assert locate_hit("src/aifix/eval/mine.py", ["aifix/eval/mine.py"])


def test_locate_hit_different_filename_misses():
    assert not locate_hit("eval/mine.py", ["src/aifix/eval/task.py"])


def test_locate_hit_same_filename_different_dir_misses():
    """目录对不上不能只靠文件名撞对——否则指标会被文件名撞车稀释。"""
    assert not locate_hit("other/mine.py", ["src/aifix/eval/mine.py"])


def test_locate_hit_none_suspect_misses():
    assert not locate_hit(None, ["src/aifix/eval/mine.py"])


def test_locate_hit_empty_suspect_misses():
    assert not locate_hit("", ["src/aifix/eval/mine.py"])


def test_locate_hit_empty_gold_entry_is_ignored():
    assert not locate_hit("mine.py", [""])


def test_locate_hit_leading_dot_slash_is_normalized():
    """模型可能带 `./` 前缀，两侧都要先规整掉再比较分段。"""
    assert locate_hit("./aifix/eval/mine.py", ["src/aifix/eval/mine.py"])
    assert locate_hit("aifix/eval/mine.py", ["./src/aifix/eval/mine.py"])


def test_locate_hit_rejects_naive_string_endswith_false_positive():
    """裸字符串 endswith 会把 `xmine.py` 误判成命中 `mine.py`（前缀被截断
    到字符中间）——按路径分段比较则不会，因为 `xmine.py` 整段都不等于
    `mine.py` 的任何一段。这条测试锁定实现必须走分段比较，不能退化回
    `str.endswith`。"""
    assert not locate_hit("xmine.py", ["src/aifix/eval/mine.py"])


def _bare_task(tid: str, origin: str = "mined") -> Task:
    """只为调度逻辑服务的任务壳 —— 字段不会被 fake 的 run_task 读到。"""
    return Task(task_id=tid, repo="/tmp/x", commit="c", base_commit="b",
                test_files=["tests/t.py"], target_test="tests/t.py::x",
                gold_files=["a.py"], origin=origin)


def _fake_run_task(seen: list, caps: list, cost: float):
    """造一个花固定钱数的 run_task 替身。

    直接控制「一个任务花多少钱」，而不是让完整修复闭环去算 —— 这几条测的
    是**调度逻辑**，不该被闭环的成本波动牵着走，也不该为此跑几分钟测试。
    """
    async def fake(task, config, model, workdir, **kw):
        seen.append(task.task_id)
        caps.append(config.budget_usd)
        return TaskResult(task_id=task.task_id, model=model, locate_hit=True,
                          suspect_file="a.py", verdict="better", attempts=1,
                          tokens=100, cost_usd=cost, violations=0)
    return fake


async def test_suite_budget_stops_dispatching(monkeypatch, tmp_path):
    """整批额度花完就停止派发；已跑的照常，未跑的记成评测故障。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    tasks = [_bare_task(f"t{i}") for i in range(4)]
    rs = await R.run_suite(tasks, AifixConfig(budget_usd=5.0), "假模型",
                           tmp_path, parallel=1, total_usd=2.0)

    assert seen == ["t0", "t1"], "花满 2.0 之后不该再派发"
    assert [r.task_id for r in rs] == ["t0", "t1", "t2", "t3"], "顺序必须保持"
    assert rs[0].error is None and rs[1].error is None
    assert all("未运行" in (r.error or "") for r in rs[2:])


async def test_skipped_tasks_do_not_count_as_failures(monkeypatch, tmp_path):
    """跳过的任务不能进比率分母 —— 否则被测系统替调度决策背锅。"""
    import aifix.eval.runner as R
    from aifix.eval.score import summarize

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    rs = await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                           AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                           parallel=1, total_usd=1.0)
    s = summarize(rs)
    assert s.tasks == 1, "只有真跑过的进分母"
    assert s.errors == 2
    assert s.fix_rate == 1.0, "跳过的不该把成功率拉低"


async def test_per_task_cap_is_clamped_to_remaining(monkeypatch, tmp_path):
    """最后一个任务只拿得到剩下的钱 —— 不能起一个付不起的活。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                      AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                      parallel=1, total_usd=1.5)
    assert caps == [1.5, 0.5], "第一个被整批剩余压到 1.5，第二个只剩 0.5"


async def test_suite_budget_gate_holds_under_concurrency(monkeypatch, tmp_path):
    """并发派发下，整批总闸必须靠「预留」而不是「事后累加」，否则同一批
    额度会被超支到并发数那么多倍。

    复现条件与实测一致：整批上限 $1.0、每个任务花 $1.0、4 个任务、
    parallel=4。修复前，`spent` 只在任务跑完后才累加，4 个并发槽位在
    派发前全部读到同一个旧值 `spent=0.0`，都误判「还有额度」，于是全部
    4 个任务都被派发、实际烧掉 $4.0 —— 4 倍超支。用 `asyncio.sleep`
    强制任务体跨越检查窗口，让 4 个协程真正交错；不 sleep 的话协程可能
    近似顺序执行，测不出竞态。
    """
    import aifix.eval.runner as R

    seen: list[str] = []

    async def fake(task, config, model, workdir, **kw):
        seen.append(task.task_id)
        await asyncio.sleep(0.05)  # 强迫 4 个协程的派发窗口互相交错
        return TaskResult(task_id=task.task_id, model=model, locate_hit=True,
                          suspect_file="a.py", verdict="better", attempts=1,
                          tokens=100, cost_usd=1.0, violations=0)

    monkeypatch.setattr(R, "run_task", fake)
    tasks = [_bare_task(f"t{i}") for i in range(4)]
    rs = await R.run_suite(tasks, AifixConfig(budget_usd=5.0), "假模型",
                           tmp_path, parallel=4, total_usd=1.0)

    total_spent = sum(r.cost_usd for r in rs)
    ran = sum(1 for r in rs if r.error is None)
    assert ran == 1, (
        f"整批上限 $1.0 只够跑 1 个花 $1.0 的任务，实际跑了 {ran} 个："
        f"{seen}")
    assert total_spent <= 1.0 + 1e-9, (
        f"整批实际花销 {total_spent} 超过上限 $1.0（容差 1e-9）")


async def test_on_done_is_never_called_while_holding_the_lock(
        monkeypatch, tmp_path):
    """三条路径（正常 / 异常 / 跳过）都必须在锁外回调 on_done。

    on_done 是用户提供的 I/O 回调（CLI 里是 print），锁内调用会把整批
    调度阻塞在一次 I/O 上，且与正常/异常两条路径的回调时机不一致。

    断言方式：monkeypatch 掉 runner 模块里的 asyncio.Lock 构造函数，把
    run_suite 内部真正用来做「预留」记账的那把锁偷渡出来；on_done 触发
    时立即读它的 locked()——如果此刻返回 True，说明 on_done 正跑在
    `async with lock:` 内部。这比「在 on_done 里 sleep(0) 后断言另一个
    任务推进了」更直接：不依赖并发调度的时序巧合，直接看临界区状态本
    身，不会写成恒真断言。
    """
    import aifix.eval.runner as R

    captured: list[asyncio.Lock] = []
    real_lock_cls = asyncio.Lock

    def _spy_lock(*a, **kw):
        lk = real_lock_cls(*a, **kw)
        captured.append(lk)
        return lk

    monkeypatch.setattr(R.asyncio, "Lock", _spy_lock)
    monkeypatch.setattr(R, "run_task", _fake_run_task([], [], cost=1.0))

    locked_at_call: list[bool] = []

    def on_done(r):
        assert captured, "没能拿到 run_suite 内部的锁引用"
        locked_at_call.append(captured[0].locked())

    tasks = [_bare_task(f"t{i}") for i in range(3)]
    # total_usd 只够第一个任务，逼第 2、3 个任务走跳过分支；三个任务都
    # 会各触发一次 on_done，凑齐「正常 + 跳过」两条路径。
    await R.run_suite(tasks, AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                      parallel=1, total_usd=1.0, on_done=on_done)

    assert len(locked_at_call) == 3, "三个任务都应该各触发一次 on_done"
    assert not any(locked_at_call), (
        f"on_done 在锁仍被持有时被调用了：{locked_at_call}")


async def test_skipped_tasks_keep_their_origin(monkeypatch, tmp_path):
    """跳过分支走的是模块级 _blank，必须把 task.origin 带上 —— 否则被
    跳过的变异任务会静默落回默认值 mined，正好是本任务要防的事。"""
    import aifix.eval.runner as R

    monkeypatch.setattr(R, "run_task", _fake_run_task([], [], cost=1.0))
    tasks = [_bare_task("t0", origin="mutated"),
            _bare_task("t1", origin="mutated")]
    # total_usd=0.0：派发前 left <= 0，两个任务都直接进跳过分支，
    # 不必依赖 run_task 的桩真的跑起来。
    rs = await R.run_suite(tasks, AifixConfig(budget_usd=5.0), "假模型",
                           tmp_path, parallel=1, total_usd=0.0)
    assert all("未运行" in (r.error or "") for r in rs)
    assert all(r.origin == "mutated" for r in rs), (
        f"跳过的结果没有带上任务的 origin：{[r.origin for r in rs]}")


async def test_exception_path_keeps_task_origin(monkeypatch, tmp_path):
    """run_task 炸掉时走的也是模块级 _blank，同样必须带上 origin。"""
    import aifix.eval.runner as R

    async def boom(task, config, model, workdir, **kw):
        raise RuntimeError("炸了")

    monkeypatch.setattr(R, "run_task", boom)
    rs = await R.run_suite([_bare_task("t0", origin="mutated")],
                           AifixConfig(), "假模型", tmp_path, parallel=1)
    assert rs[0].error is not None
    assert rs[0].origin == "mutated"


async def test_no_suite_budget_leaves_config_untouched(monkeypatch, tmp_path):
    """不给整批上限时全跑，且不改写每任务额度。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=99.0))
    rs = await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                           AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                           parallel=1)
    assert seen == ["t0", "t1", "t2"]
    assert caps == [5.0, 5.0, 5.0], "没有整批闸时不该压低每任务额度"
    assert all(r.error is None for r in rs)
