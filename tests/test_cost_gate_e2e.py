"""成本闸的端到端：从配置到报告，钱真的被拦住了。"""
import json
import subprocess
from pathlib import Path

import pytest
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


def _text(t, usage=Usage(10, 5, 15)):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=usage)]


def _tool(name, args, usage=Usage(10, 5, 15)):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=usage)]


# 配 _PRICEY 时成本 = 输入 + 输出（每千 token 1000 美元）。取几个互不相同的
# 数额，好让「哪一笔钱花在哪儿」在断言里可以逐笔对上。
_USD10 = Usage(6, 4, 10)
_USD15 = Usage(10, 5, 15)
_USD30 = Usage(20, 10, 30)


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


_TWO_BUGS_SRC = '''def add(a, b):
    return a - b        # bug: 应为 a + b


def mul(a, b):
    return a + b        # bug: 应为 a * b
'''

_TWO_BUGS_TEST = '''from calc import add, mul


def test_add():
    assert add(2, 3) == 5


def test_mul():
    assert mul(2, 3) == 6
'''

# 只修 add，mul 原样留红。必须比 _PATCH 多带一行尾部上下文：_PATCH 的
# `@@ -1,2 +1,2 @@` 在两行的文件里恰好顶到 EOF，套到六行的文件上
# `git apply --check` 会直接判「patch does not apply」。
_TWO_BUGS_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b

"""


@pytest.fixture
def two_bugs_repo(tmp_path: Path) -> Path:
    """两个失败用例的仓库 —— 队列里必须有「下一个」，中止路径才走得到。

    baseline_node 用 sorted(ids) 建队列，所以顺序是确定的：
    test_add 先、test_mul 后。_PATCH 只修 add，于是 test_mul 一直是红的。
    """
    repo = tmp_path / "two"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_TWO_BUGS_SRC, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        _TWO_BUGS_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@example.com"],
                ["config", "user.name", "t"],
                ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True,
                       capture_output=True, text=True)
    return repo


async def test_abort_does_not_credit_a_failure_that_never_ran(two_bugs_repo):
    """预算中止时，刚从队列 pop 出来、一次都没跑过的 failure 不能被记成「已修复」。

    这条路径只有两个以上 failure 才走得到，而本分支其余端到端测试都是单
    failure 仓库 —— 于是回归一直没人看见。时序是：

      第 1 轮  test_add 判 BETTER → verify_node 置 current=None / attempt=0
              / verdict="better"
      第 2 轮  循环顶部先 pop 出 test_mul、attempt=1，**然后**才发现预算耗尽
              → 补录 test_mul

    补录时 state["verdict"] 读到的仍是 test_add 的判定。test_mul 一个字节
    没改、detect/fix/verify 一次没跑、在它身上花了 $0，却会被记成「已修复」。
    后果有两层：报告谎报修复数；eval 的 run_task 按 test_id 取到这一行，
    TaskResult.verdict 直接抬高整批的修复成功率。

    金额安排（配 _PRICEY，成本 = 输入 + 输出）：
      上限 $50；detect $10；fix 两次调用 $15（apply_patch）+ $30（收尾文本）。
      第 1 轮分给 test_add 的额度 ≥ $20 > $15，所以 apply_patch 一定跑得到、
      test_add 确实被修好；一轮下来共花 $55 ≥ $50，第 2 轮顶部必然中止。
    """
    fixer = _Scripted([_tool("apply_patch",
                             json.dumps({"diff": _TWO_BUGS_PATCH}),
                             usage=_USD15),
                       _text("已修复", usage=_USD30)])
    state = await run_once(
        two_bugs_repo, AifixConfig(budget_usd=50.0, price_map=_PRICEY),
        run_id="two1",
        detector_client=_Scripted([_text(_DIAG, usage=_USD10)]),
        fixer_client=fixer)

    assert state["abort_kind"] == "usd", "前提没成立：这一轮不是被美元闸掐断的"
    fixed = [r for r in state["results"]
             if r["test_id"] == "tests/test_calc.py::test_add"]
    assert [r["verdict"] for r in fixed] == ["better"], (
        "前提没成立：第一个 failure 应该先被真正修好，才轮得到第二个被 pop")

    never_ran = [r for r in state["results"]
                 if r["test_id"] == "tests/test_calc.py::test_mul"]
    assert [r["verdict"] for r in never_ran] != ["better"], (
        "test_mul 压根没跑过 detect/fix/verify，在它身上花了 $0，"
        "不能借上一个 failure 的 verdict 记成「已修复」")


async def test_guard_giveup_shows_in_report(buggy_repo):
    """连续空 diff 放弃后，报告里的中止原因要能区分出是哪一条守卫。"""
    state = await run_once(
        buggy_repo, AifixConfig(fix_guard_retries=5, guard_giveup_limit=2),
        run_id="giveup1",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_text("已修复")]))
    assert "empty_diff_giveup" in state["report_md"]
