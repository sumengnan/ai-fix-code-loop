"""复现测试没碰过本项目代码时，报告必须刺眼地说出来。

那道闸只退回重写一次（见 issue/handle.py），第二次仍不过就放行 —— 误报的
成本必须封顶。代价是：放行而不出声，与「这条复现很干净」在读者眼里一模一样，
而这次判定所依据的那条测试可能根本测不到行为（ai-learning-helper#95 的形状）。
"""
from aifix.nodes.report import render_report


def _state(**over):
    return {
        "run_id": "r1", "adapter_names": ["pytest"], "branch": "aifix/r1",
        "baseline_ids": ["tests/t.py::x"], "spent_tokens": 100,
        "spent_cny": 1.0, "config": None, "abort": None,
        "results": [{"test_id": "tests/t.py::x", "verdict": "better",
                     "attempts": 1, "abort_reason": None}],
        "signals": [],
    } | over


def test_it_says_so_when_the_reproduction_never_touched_the_code():
    md = render_report(_state(repro_untouching=True))
    assert "没有 import 本项目的任何模块" in md


def test_it_tells_the_reader_what_to_do():
    """「可能有问题」不是可执行的下一步。撤掉补丁看它变不变红，才是。"""
    md = render_report(_state(repro_untouching=True))
    assert "撤掉" in md and "变红" in md


def test_it_admits_the_judgement_is_heuristic():
    """不说清判据是启发式的，一次误报就会让人从此无视这一节。"""
    md = render_report(_state(repro_untouching=True))
    assert "subprocess" in md


def test_a_clean_run_gets_no_such_section():
    """恒定出现的小节等于没有小节 —— 人会学会跳过整块。"""
    assert "没有 import 本项目" not in render_report(_state())
    assert "没有 import 本项目" not in render_report(_state(repro_untouching=False))
