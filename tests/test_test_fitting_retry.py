"""补丁里出现目标测试的字面量时，先退回去让它重写一遍。

`hardcoded_literals` 此前只是一条信号：判 BETTER 照样交付，只在报告里提一句
「值得多看一眼」。它是四类静态信号里唯一**直接**对着规格套利的一条，而规格
套利恰恰是「测试绿了但 bug 没修」的主要形状——必要性反查也抓不到它（那段硬
编码确实让目标转绿，撤掉就红，按「有没有贡献」判它是必要的）。

升级成闸而不升级裁判模型的那一条：这一条是**确定性**的静态比对，不是模型的
看法。让它参与判定不违反「判定权不交给模型」。

**不是否决，是退回重写**：还有 attempt 额度就回滚重来，并把理由喂回去；额度
用尽仍然交付并照旧标注——一条永远拦死的闸会把「唯一能修好的补丁」也扔掉。
"""
import json

import pytest

from aifix.adapters.base import Failure, Verdict
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import new_state
from aifix.nodes.preflight import preflight_node
from aifix.nodes.verify import _REFIT_NOTE, verify_node

_TID = "tests/test_calc.py::test_add"

# 目标测试里的字面量 5 会被 distinctive_literals 采到；补丁新增的判断用了它。
_FITTED = """def add(a, b):
    if a == 2 and b == 3:
        return 5
    return a - b
"""

_HONEST = """def add(a, b):
    return a + b
"""


def _state(repo, wt, **over):
    st = new_state(repo, AifixConfig(**over), run_id="r1")
    st.update(preflight_node(st))
    st["worktree_path"] = str(wt.path)
    st["baseline_ids"] = [_TID]
    st["queue"] = []
    st["current"] = _TID
    st["attempt"] = 1
    st["touched"] = ["calc.py"]
    # file 必须给：signals 靠它读目标测试的源码，没有它这一类信号恒空。
    st["_failures"] = {_TID: Failure(
        test_id=_TID, classname="tests.test_calc", name="test_add",
        message="assert -1 == 5", trace="E assert -1 == 5",
        file="tests/test_calc.py")}
    return st


async def test_fitted_patch_is_sent_back_instead_of_delivered(buggy_repo):
    """贴着测试写的补丁：测试确实绿了，但不交付，退回去重写。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(_FITTED, encoding="utf-8")
        out = await verify_node(st)

        # 不返回 current 即「保持不变」，同一个 failure 继续 —— 与既有的重试
        # 分支同一个契约（收尾那条才显式写 current: None）。
        assert "current" not in out, "同一个 failure 要继续，不能收尾"
        assert out["attempt"] == 2, "退回重写要消耗一次 attempt"
        assert out.get("retry_note"), "必须把理由喂回给 fixer"
        assert "字面量" in out["retry_note"]
        # 工作区必须回滚干净，否则下一轮是在这个补丁上继续改
        assert "a == 2 and b == 3" not in (
            wt.path / "calc.py").read_text(encoding="utf-8")


async def test_an_honest_patch_is_untouched(buggy_repo, fixed_source):
    """反向对照：没有用到测试字面量的补丁照常交付，这道闸不能误伤。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        (wt.path / "calc.py").write_text(_HONEST, encoding="utf-8")
        out = await verify_node(st)

        assert out["verdict"] == Verdict.BETTER.value
        assert out["current"] is None
        assert not out.get("retry_note")


async def test_the_last_attempt_delivers_anyway(buggy_repo):
    """额度用尽仍然交付并照旧标注。

    一条永远拦死的闸会把「唯一能修好的那个补丁」也扔掉，那比交付一个可疑补丁
    更糟——后者至少在报告里被标了出来，人看得见。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        st["attempt"] = st["config"].max_attempts
        (wt.path / "calc.py").write_text(_FITTED, encoding="utf-8")
        out = await verify_node(st)

        assert out["verdict"] == Verdict.BETTER.value
        assert out["current"] is None
        assert not out.get("retry_note")
        # 信号照旧要出现在报告里
        assert out["signals"], "交付了就必须带上那条信号"
        assert out["signals"][-1]["hardcoded_literals"]


async def test_the_gate_can_be_turned_off(buggy_repo):
    """留一个开关：这道闸会多花一轮 fixer 的钱。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, test_fitting_retry=False)
        (wt.path / "calc.py").write_text(_FITTED, encoding="utf-8")
        out = await verify_node(st)

        assert out["verdict"] == Verdict.BETTER.value
        assert out["current"] is None


def test_the_reason_reaches_the_fixer():
    """退回必须把理由送到 fixer 眼前，否则它只是空转一轮。

    直接考 build_initial_messages：退回的理由要出现在开场白里，且排在诊断
    之后 —— 它讲的是上一轮具体错在哪，比模型自己猜的那份诊断确定得多。
    """
    from aifix.adapters.base import Failure
    from aifix.agents.fixer import build_initial_messages

    f = Failure(test_id=_TID, classname="c", name="n",
                message="assert -1 == 5", trace="t")
    note = _REFIT_NOTE.format(conds="  - if a == 2 and b == 3")
    msgs = build_initial_messages(f, None, retry_note=note)
    body = msgs[0].content

    assert "字面量" in body
    assert "if a == 2 and b == 3" in body
    assert body.index("traceback") < body.index("字面量"), "理由要排在最后"


def test_no_note_means_no_placeholder():
    """反向对照：没被退回时不该凭空多出一段话。"""
    from aifix.adapters.base import Failure
    from aifix.agents.fixer import build_initial_messages

    f = Failure(test_id=_TID, classname="c", name="n", message="m", trace="t")
    assert "退回" not in build_initial_messages(f, None)[0].content
