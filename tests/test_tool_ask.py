"""`ask_user` 的三道硬约束。

这个工具最大的风险不是没人用，是**被滥用**：提问比修 bug 便宜得多，不设限
的话模型会拿它当出口。所以三条都是代码里的硬判，不是提示词里的一句叮嘱 ——
提示词管不住一个正在找退路的模型。
"""
import pytest
from harness.tools.base import ToolError

from aifix.tools.ask import AskUserTool, Pending

_Q = "购物车为空时 most_expensive() 应该怎么办？"
_OPTS = ["返回 None（当前行为）", "抛 ValueError", "返回 0"]


def _tool(seen=("read_file",), pending=None, test_id="t::x"):
    return AskUserTool(pending or Pending(), set(seen), test_id)


async def test_a_question_is_recorded(caplog):
    p = Pending()
    t = AskUserTool(p, {"read_symbol"}, "tests/test_cart.py::test_empty")
    out = await t.run(t.Params(question=_Q, options=_OPTS))
    assert p.asked and p.question == _Q and p.options == _OPTS
    assert p.test_id == "tests/test_cart.py::test_empty"
    # 回执必须明确叫停：不叫停的话模型会接着改，而那正是「信息不全」时
    # 最不该发生的事
    assert "停下" in out


async def test_asking_before_reading_anything_is_refused():
    """**第一道**：一次工具都没调就说「信息不全」，多半是它自己没查。

    这是三条里最容易被绕过的一条，也是最值钱的一条 —— 不设它的话，
    模型在第一步就能用提问把整个任务推回给人。
    """
    t = _tool(seen=())
    with pytest.raises(ToolError, match="还没有读过"):
        await t.run(t.Params(question=_Q, options=_OPTS))


async def test_running_tests_does_not_count_as_having_read_code():
    """跑一遍测试不等于看过代码 —— 而 run_tests 恰恰是最容易用来充数的动作。"""
    t = _tool(seen=("run_tests",))
    with pytest.raises(ToolError, match="还没有读过"):
        await t.run(t.Params(question=_Q, options=_OPTS))


@pytest.mark.parametrize("tool_name", ["read_file", "read_symbol", "grep",
                                       "list_files"])
async def test_any_genuine_read_unlocks_it(tool_name):
    """反向对照：四个读工具**都**算数。漏掉任何一个，这道约束就变成了
    「只有用某个特定工具读过才准问」——那不是它要表达的意思。"""
    t = _tool(seen=(tool_name,))
    await t.run(t.Params(question=_Q, options=_OPTS))


async def test_only_one_question_per_run():
    """**第二道**：不设上限的话，提问是比修 bug 便宜得多的动作。"""
    p = Pending()
    t = AskUserTool(p, {"read_file"})
    await t.run(t.Params(question=_Q, options=_OPTS))
    with pytest.raises(ToolError, match="一次只能问一个"):
        await t.run(t.Params(question="另一个问题", options=["甲", "乙"]))
    # 第一个问题不许被覆盖 —— 覆盖的话人回答的是它没看到的那个问题
    assert p.question == _Q


@pytest.mark.parametrize("opts", [[], ["只有一个"], ["一", "二", "三", "四", "五"]])
async def test_the_option_count_is_enforced(opts):
    """**第三道**：必须是选项，而且是 2-4 个。

    一个选项不是选择题；五个以上人读不过来，而且多半说明模型没想清楚。
    这一条由 pydantic 在参数层挡住，模型拿到的是结构化的报错。
    """
    t = _tool()
    with pytest.raises(Exception):
        t.Params(question=_Q, options=opts)


async def test_duplicate_options_are_refused():
    """选项重复说明模型没在提供真正的选择 —— 人选哪个都一样，
    那这次提问白白花掉了一整轮往返。"""
    t = _tool()
    with pytest.raises(ToolError, match="重复"):
        await t.run(t.Params(question=_Q, options=["返回 None", " 返回 None "]))


async def test_an_empty_question_is_refused():
    t = _tool()
    with pytest.raises(Exception):
        t.Params(question="", options=_OPTS)


async def test_the_seen_set_is_shared_not_copied():
    """接线检查：工具持有的必须是 fix_node **那一个**集合对象。

    传副本的话这里永远看到空集，第一道约束会把每一次提问都拦死 —— 而症状是
    「模型说它读过代码，工具说它没读」，那种矛盾极难从日志里看出来。
    """
    seen: set[str] = set()
    p = Pending()
    t = AskUserTool(p, seen)
    seen.add("read_file")                    # 建好之后才加，模拟真实时序
    await t.run(t.Params(question=_Q, options=_OPTS))
    assert p.asked
