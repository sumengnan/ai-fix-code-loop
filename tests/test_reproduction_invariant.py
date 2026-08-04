"""复现步除了那条测试，还要说出**它钉的是什么规则**。

提示词里「只写一条测试函数，不要顺手补别的用例」是对的（宽测试更容易写错），
但它同时意味着判据只有一个样本点。于是贴着那个样本写的补丁和真修好的补丁，
在判定眼里一模一样。

出路不是让它多写用例，而是让它额外用一句话说清这条测试在钉什么不变量，把这
句话交给 fixer 和读报告的人。

**它绝不进判定路径**：这句话是模型产出的，让它参与判定就等于把判定权交回给
模型，而那是这个项目从一开始就拒绝的东西。判定仍然只看测试结果。
"""
import json

from aifix.adapters.base import Failure
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.agents.fixer import build_initial_messages
from aifix.agents.reproducer import (SYSTEM_PROMPT, Reproduction,
                                     parse_reproduction_ex)

_A = PytestAdapter()

_CODE = ("from calc import add\n\n\n"
         "def test_add():\n    assert add(2, 3) == 5\n")


def _raw(**over) -> str:
    return json.dumps({
        "can_reproduce": True,
        "test_file": "tests/test_add.py",
        "test_code": _CODE,
        "target_test_id": "tests/test_add.py::test_add",
        "missing_info": [],
    } | over)


# ------------------------------------------------------------ 字段

def test_the_invariant_is_carried_through():
    r, why = parse_reproduction_ex(
        _raw(invariant="add 必须返回两个入参之和，与具体取值无关"), _A.is_test_path)
    assert why == ""
    assert "两个入参之和" in r.invariant


def test_a_missing_invariant_is_not_fatal():
    """旧格式仍要能解析。

    这句话是**给人和 fixer 的**，不是判据。因为模型少写一句话就把一条能用的
    复现整个丢掉，是拿掉一个有价值的产出去换一个装饰性的字段。
    """
    r, why = parse_reproduction_ex(_raw(), _A.is_test_path)
    assert why == "" and r is not None
    assert r.invariant == ""


def test_giving_up_needs_no_invariant():
    r, why = parse_reproduction_ex(json.dumps({
        "can_reproduce": False, "missing_info": ["没有给出期望输出"]}),
        _A.is_test_path)
    assert why == "" and r.can_reproduce is False


# ------------------------------------------------------------ 提示词

def test_the_prompt_asks_for_the_rule_not_another_case():
    """要的是一句规则，不是再写一条用例 —— 后者与「只写一条」直接冲突。"""
    assert "invariant" in SYSTEM_PROMPT
    assert "规则" in SYSTEM_PROMPT
    assert "只写一条测试函数" in SYSTEM_PROMPT, "原来那条硬约束不能被顶掉"


# ------------------------------------------------------------ 送到 fixer

def test_the_rule_reaches_the_fixer():
    """fixer 要照着规则改，而不是照着那一个样本改。"""
    f = Failure(test_id="tests/test_add.py::test_add", classname="c",
                name="test_add", message="assert -1 == 5", trace="t")
    body = build_initial_messages(
        f, None, invariant="add 必须返回两个入参之和，与具体取值无关")[0].content
    assert "两个入参之和" in body


def test_no_rule_means_no_placeholder():
    f = Failure(test_id="t::x", classname="c", name="x", message="m", trace="t")
    body = build_initial_messages(f, None)[0].content
    assert "这条测试钉的规则" not in body


def test_the_rule_is_marked_as_the_models_own_words():
    """必须标明它是模型写的。

    不标的话，fixer 会把它当成规格来读 —— 而它只是上一个模型的一句话，
    和那份诊断同一档证据强度（诊断那边就明确写着「仅供参考」）。
    """
    f = Failure(test_id="t::x", classname="c", name="x", message="m", trace="t")
    body = build_initial_messages(f, None, invariant="必须是幂等的")[0].content
    assert "仅供参考" in body or "自己读代码" in body


# ------------------------------------------------------------ 不进判定

def test_the_invariant_never_reaches_the_verdict():
    """判定只看测试结果。这里从反面钉：Reproduction 里这个字段是纯文本，
    没有任何布尔/枚举能被判定路径消费。"""
    r = Reproduction(can_reproduce=True, test_file="tests/t.py",
                     test_code=_CODE, target_test_id="tests/t.py::x",
                     invariant="随便写点什么")
    assert isinstance(r.invariant, str)
