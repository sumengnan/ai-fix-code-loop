import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.fix import fix_node
from aifix.nodes.preflight import preflight_node

_TID = "tests/test_calc.py::test_add"

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

# 整文件重写：删掉原来 2 行，塞进 400 行。numstat 记 402 行改动。
_HUGE = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,400 @@\n"
         "-def add(a, b):\n"
         "-    return a - b        # bug: 应为 a + b\n"
         + "".join(f"+line{i}\n" for i in range(400)))


class _Scripted:
    """按轮次回放。最后一轮会被无限重复 —— 所以每个 tool 轮后必须跟一个
    text 轮，否则 AgentLoop 拿到工具结果后又收到同一个 tool_call，
    会一直重复到 loop_detect_window 熔断，calls 数就没法断言了。"""

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


def _state(buggy_repo, wt, **over):
    cfg = AifixConfig(**over)
    st = new_state(buggy_repo, cfg, run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["baseline_ids"] = [_TID]
    st["current"] = _TID
    st["attempt"] = 1
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="c", name="test_add",
        message="assert -1 == 5", trace="E assert -1 == 5")}
    return st


async def test_empty_diff_triggers_retry_with_feedback(buggy_repo):
    """只说「已修复」却没调 apply_patch —— 必须重试并把这件事告诉模型。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        client = _Scripted([_text("已修复"),
                            _tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("这次真改了")])
        out = await fix_node(st, client=client)
        assert out["touched"] == ["calc.py"], "第二轮的补丁必须真的落地"
        assert out["guard_hits"] == ["empty_diff"]
        assert out["abort_reason"] is None
        assert client.calls == 3, "空 diff 后应再给模型一次机会"


async def test_empty_diff_exhausts_retries(buggy_repo):
    """一直不改就放弃，并记下原因。

    guard_giveup_limit 特意调大到跳出 fix_guard_retries+1 的轮数之外，
    这样测的是「重试次数耗尽」本身，不会被同守卫连续放弃规则抢先命中
    （那条规则由 test_same_guard_twice_gives_up 等用例单独覆盖）。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=1, guard_giveup_limit=3)
        client = _Scripted([_text("已修复")])
        out = await fix_node(st, client=client)
        assert client.calls == 2
        assert out["touched"] == []
        assert out["abort_reason"] == "empty_diff"
        assert out["guard_hits"] == ["empty_diff", "empty_diff"]


async def test_huge_diff_rolls_back_and_retries(buggy_repo):
    """改动过大判为整文件重写：回滚后重来，不能让垃圾留在工作区。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, max_diff_lines=50)
        client = _Scripted([
            _tool("apply_patch", json.dumps({"diff": _HUGE})),
            _text("重写完毕"),
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
            _text("这次只改一行"),
        ])
        out = await fix_node(st, client=client)
        assert "huge_diff" in out["guard_hits"]
        content = (wt.path / "calc.py").read_text(encoding="utf-8")
        assert "line399" not in content, "巨型补丁必须被回滚"
        assert "a + b" in content, "回滚后第二次补丁应正常应用"
        assert out["touched"] == ["calc.py"]
        assert out["diff_lines"] == 2, "记录的是回滚后重打的那个小补丁"


async def test_huge_diff_reports_rolled_back_size_not_stale(buggy_repo):
    """守卫用尽时 diff_lines 必须是 0（已回滚），不是回滚前的陈旧值。

    观测数据撒谎比没有观测更糟 —— trace 里写着改了 402 行，
    而工作区一个字节都没变。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, max_diff_lines=50, fix_guard_retries=0)
        client = _Scripted([
            _tool("apply_patch", json.dumps({"diff": _HUGE})),
            _text("重写完毕"),
        ])
        out = await fix_node(st, client=client)
        assert out["abort_reason"] == "huge_diff"
        assert out["diff_lines"] == 0
        assert out["touched"] == []


async def test_good_patch_passes_guards_in_one_shot(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("已修复")])
        out = await fix_node(st, client=client)
        assert out["guard_hits"] == []
        assert out["abort_reason"] is None
        assert out["diff_lines"] == 2
        assert client.calls == 2, "一次过，没有守卫重试"


async def test_fix_stops_when_failure_usd_budget_exhausted(buggy_repo):
    """单 failure 的美元额度用完 —— 不再发起新的模型调用。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, price_map={"gpt-4o-mini": [1000.0, 1000.0]})
        st["failure_usd_budget"] = 0.001      # 一次调用就必然越线
        client = _Scripted([_text("我想想"),
                            _tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("改好了")])
        out = await fix_node(st, client=client)
        assert client.calls == 1, "越线后不该再发起第二次调用"
        assert out["cost_capped"] is True


async def test_zero_usd_budget_is_a_closed_gate_not_an_absent_one(buggy_repo):
    """额度恰好为 0.0 = 已经扣光，一次调用都不许发起 —— 不是「不设闸」。

    旧写法是 `state.get("failure_usd_budget") or None`，而 `0.0 or None`
    求值成 None，于是闸最该拦住的那一刻反而完全不拦。0.0 不是理论边界：
    额度按「扣掉 detect 的花费之后的剩余」算，detect 恰好花光时就是 0.0。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, price_map={"gpt-4o-mini": [1000.0, 1000.0]})
        st["failure_usd_budget"] = 0.0
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("已修复")])
        out = await fix_node(st, client=client)
        assert client.calls == 0, "额度为 0 时一次模型调用都不该发起"
        assert out["cost_capped"] is True
        assert out["diff_lines"] == 0


async def test_fix_without_usd_budget_runs_normally(buggy_repo):
    """未分配额度（None）才是「不设美元闸」—— 与上面的 0.0 是两回事。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        assert st["failure_usd_budget"] is None, (
            "new_state 的默认必须是 None：build_graph 那条路径不填这个字段，"
            "默认成 0.0 会被读成「额度已扣光」，fix 从此一次调用都发不出去")
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("已修复")])
        out = await fix_node(st, client=client)
        assert out["cost_capped"] is False
        assert out["diff_lines"] == 2


async def test_same_guard_twice_gives_up(buggy_repo):
    """连续两次空 diff = 同一堵墙撞两回，别再撞第三次。

    实测两个真实模型都在这里各烧了 51~52 万 token 却一个字没改。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=5, guard_giveup_limit=2)
        client = _Scripted([_text("已修复")])       # 永远不改任何文件
        out = await fix_node(st, client=client)
        assert client.calls == 2, "第二次空 diff 即放弃，不该跑满 6 轮"
        assert out["abort_reason"] == "empty_diff_giveup"
        assert out["guard_hits"] == ["empty_diff", "empty_diff"]


async def test_alternating_guards_do_not_give_up(buggy_repo):
    """交替触发说明模型在换思路，值得再给一次机会。"""
    big = "".join(f"+line{i}\n" for i in range(400))
    huge = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,400 @@\n"
            "-def add(a, b):\n"
            "-    return a - b        # bug: 应为 a + b\n" + big)
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, max_diff_lines=50,
                    fix_guard_retries=3, guard_giveup_limit=2)
        client = _Scripted([
            _text("先不改"),                                    # empty_diff
            _tool("apply_patch", json.dumps({"diff": huge})),   # huge_diff
            _text("重写完毕"),
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
            _text("这次只改一行"),
        ])
        out = await fix_node(st, client=client)
        assert out["guard_hits"] == ["empty_diff", "huge_diff"]
        assert out["abort_reason"] is None, "交替触发不该提前放弃"
        assert out["diff_lines"] == 2


async def test_giveup_limit_one_gives_up_immediately(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=5, guard_giveup_limit=1)
        client = _Scripted([_text("已修复")])
        out = await fix_node(st, client=client)
        assert client.calls == 1
        assert out["abort_reason"] == "empty_diff_giveup"
