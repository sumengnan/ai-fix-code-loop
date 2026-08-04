"""报告要把「这条复现测试钉的是什么规则」亮出来。

复现测试只有一个样本点，判定也只看它。补丁扛过了那一个样本不等于修好了 ——
最后那道闸是人，而人要判断「这个 diff 是不是真的按规则改的」，就得看见那条
规则。只写「已修复」等于把唯一能做这个判断的人蒙住眼睛。

它是模型写的一句话，所以措辞必须标明出处：与判定同等的排版会让人把它当成
规格。
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


def test_the_rule_shows_up_in_the_report():
    md = render_report(_state(invariant="返回值必须是两个入参之和，与具体取值无关"))
    assert "两个入参之和" in md


def test_the_rule_is_attributed_to_the_model():
    """不标出处，它读起来就和「适配器」「分支」一样是事实。"""
    md = render_report(_state(invariant="必须幂等"))
    i = md.index("必须幂等")
    around = md[max(0, i - 200):i + 200]
    assert "复现" in around and ("仅供参考" in around or "模型" in around)


def test_no_rule_means_no_empty_section():
    """恒定出现的空小节会让人学会跳过整块。"""
    md = render_report(_state())
    assert "钉的规则" not in md


def test_the_rule_does_not_look_like_a_verdict():
    """判定只看测试结果。这条不能出现在成绩单那几行里。"""
    md = render_report(_state(invariant="必须幂等"))
    assert md.index("- 修复：") < md.index("必须幂等") or "修复" not in md
