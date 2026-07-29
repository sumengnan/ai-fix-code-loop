"""复现测试的生成与红检。

红检那几条**真跑 pytest**：断言的是「收集失败长什么样」「用例失败长什么样」，
手写的 JUnit 只能证明我们理解得自洽，证明不了 pytest 真的这么写（全局约束 4）。
"""
import json
from pathlib import Path

from harness.llm.base import StreamChunk
from harness.sandbox.local import LocalSandbox
from harness.usage import Usage

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.reproduce import (build_reproduce_registry, red_check, reproduce,
                             write_reproduction)

_OK_JSON = json.dumps({
    "can_reproduce": True,
    "test_file": "tests/test_issue_1.py",
    "test_code": "from calc import add\n\n\ndef test_sum():\n    assert add(2, 3) == 5\n",
    "target_test_id": "tests/test_issue_1.py::test_sum",
    "missing_info": [],
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


# ---------------------------------------------------------------- 能力面

async def test_reproduce_registry_has_no_write_tools(tmp_path):
    """reproducer 只读。

    这是「守卫一行不用改」那条决策的钉子：现有的「不许改测试文件」守卫查的是
    **agent 的工具调用**，而复现测试是由确定性代码写下去的，压根不经过工具面。
    一旦 reproducer 拿到 apply_patch，它就能直接改产品代码——而那条路径上一道
    守卫都没有，整条推理当场失效。

    run_tests 同样不给：让它自己跑测试，「这条测试红不红」的判定权就交到了
    模型手里，而红检是这一步唯一的确定性证据。
    """
    reg = build_reproduce_registry(LocalSandbox(workspace=str(tmp_path)),
                                   PytestAdapter())
    names = {t.name for t in reg.tools()}
    assert "apply_patch" not in names
    assert "run_tests" not in names
    # 反向对照：不是因为注册表恰好是空的
    assert "read_file" in names and "grep" in names


# ---------------------------------------------------------------- 红检

async def test_red_check_accepts_a_genuine_assertion_failure(buggy_repo):
    """真断言失败：用例跑到了、断言没过。这才是有信息量的红。"""
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_calc.py::test_add")
    assert ok is True, reason


async def test_red_check_rejects_a_test_that_passes(buggy_repo):
    """在当前代码上就绿的测试约束力为零 —— 打回，且不必惊动人。"""
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_calc.py::test_identity")
    assert ok is False
    assert "没有失败" in reason


async def test_red_check_rejects_a_collection_error(buggy_repo):
    """红得空：import 不到东西也是红，但它复现的是模型自己的笔误。

    新功能的测试红在 ImportError 上是常态（函数还不存在）；修 bug 不是——
    产品代码就在那儿，import 失败说明模型猜错了模块。这一类当成「复现成功」
    的后果是，fixer 会被派去修一个根本不存在的模块。
    """
    (buggy_repo / "tests" / "test_typo.py").write_text(
        "import calculator_typo\n\n\ndef test_x():\n    assert False\n",
        encoding="utf-8")
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_typo.py::test_x")
    assert ok is False
    assert "收集" in reason
    # 反向对照：不能被读成「没有失败」——那句话会把人指向完全错误的方向
    assert "没有失败" not in reason


async def test_red_check_rejects_an_id_that_matches_nothing(buggy_repo):
    """node id 压根不存在时 pytest 收集不到用例、退出码 4，报告可能一个
    结果都没有。这与「测试红了」必须分得开。"""
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_calc.py::test_does_not_exist")
    assert ok is False
    assert reason


# ---------------------------------------------------------------- 落盘

def test_write_reproduction_creates_missing_parent_dirs(tmp_path, ):
    """模型完全可以给出 tests/regression/test_x.py，而那个子目录未必存在。
    不建父目录的话，落盘会以 FileNotFoundError 裸穿出去。"""
    from aifix.agents.reproducer import Reproduction
    r = Reproduction(can_reproduce=True, test_file="tests/regression/test_x.py",
                     test_code="def test_x():\n    assert False\n",
                     target_test_id="tests/regression/test_x.py::test_x")
    p = write_reproduction(tmp_path, r)
    assert p.read_text(encoding="utf-8").startswith("def test_x")


# ---------------------------------------------------------------- 生成

async def test_reproduce_returns_the_parsed_reproduction(buggy_repo):
    cfg = AifixConfig()
    out = await reproduce(buggy_repo, PytestAdapter(), cfg,
                          "add 算错了", "add(2,3) 返回 -1，期望 5",
                          client=_Scripted([_text(_OK_JSON)]))
    assert out.reproduction is not None
    assert out.reproduction.target_test_id == "tests/test_issue_1.py::test_sum"
    assert out.tokens > 0


async def test_reproduce_reports_missing_info_as_the_reason(buggy_repo):
    """信息不足是一条**结论**，不是错误 —— 它要原样回帖给人看。"""
    raw = json.dumps({"can_reproduce": False,
                      "missing_info": ["没说触发的输入", "没说期望输出"]})
    out = await reproduce(buggy_repo, PytestAdapter(), AifixConfig(),
                          "有 bug", "反正就是不对",
                          client=_Scripted([_text(raw)]))
    assert out.reproduction is not None
    assert out.reproduction.can_reproduce is False
    assert "没说触发的输入" in out.reason


async def test_reproduce_degrades_to_none_on_unparseable_output(buggy_repo):
    """解析不了不抛异常：上层据此走「写不出复现」通路，回帖说明。"""
    out = await reproduce(buggy_repo, PytestAdapter(), AifixConfig(),
                          "t", "b", client=_Scripted([_text("我觉得吧……")]))
    assert out.reproduction is None
    assert out.reason
