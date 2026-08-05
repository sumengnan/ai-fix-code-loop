"""复现测试的生成与红检。

红检那几条**真跑 pytest**：断言的是「收集失败长什么样」「用例失败长什么样」，
手写的 JUnit 只能证明我们理解得自洽，证明不了 pytest 真的这么写（全局约束 4）。
"""
import json

import pytest

from harness.llm.base import StreamChunk, ToolCallDelta
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


def _tool_call(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
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
    # 白名单，等号：**每加一条写入路径都得在这里过一次**。2026-07-31 加了
    # edit_file，如果这条断言只写「apply_patch not in names」，新路径就会
    # 悄悄溜进 reproducer 的能力面 —— 而上面那整段推理会在无人察觉时失效。
    assert names == {"read_file", "read_symbol", "list_files", "grep"}
    assert "apply_patch" not in names and "edit_file" not in names
    assert "run_tests" not in names


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


# ------------------------------------------- 红检：红在模型自己的笔误上

async def test_red_check_rejects_a_nameerror_from_the_models_own_typo(
        buggy_repo):
    """**这是 issue #9 真跑里漏过去的那一条。**

    模型用了 `pytest.raises` 却没 `import pytest`。前三道闸全部放行——模块
    import 得好好的（不是收集错误）、用例跑了、用例也确实红了——于是一条为
    模型自己的笔误而红的测试被当成了合法复现，fixer 对着假靶子烧掉
    $1.45 / 468k tokens，两轮都引入回归。
    """
    (buggy_repo / "tests" / "test_repro.py").write_text(
        "from calc import add\n\n\n"
        "def test_x():\n"
        "    with pytest.raises(ValueError):\n"
        "        add(1, 2)\n",
        encoding="utf-8")
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_repro.py::test_x")
    assert ok is False, "红在自己的 NameError 上，不是复现"
    assert "NameError" in reason
    assert "import pytest" in reason
    # 反向对照：这三句都会把人指向完全错误的方向
    assert "没有失败" not in reason
    assert "收集" not in reason
    assert "没有跑出任何结果" not in reason


async def test_red_check_accepts_a_nameerror_raised_by_product_code(
        buggy_repo):
    """判据是**异常抛在哪**，不是异常是什么类型。

    产品代码里真的引用了未定义的名字，那是货真价实的缺陷，NameError 正是它
    该有的样子。按类型一刀切会把这种真缺陷连同笔误一起打回——而它恰恰是最
    需要被修的那种。
    """
    (buggy_repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\n"
        "def boom():\n    return undefined_thing\n",
        encoding="utf-8")
    (buggy_repo / "tests" / "test_repro.py").write_text(
        "import calc\n\n\ndef test_x():\n    calc.boom()\n",
        encoding="utf-8")
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_repro.py::test_x")
    assert ok is True, reason


async def test_red_check_still_accepts_a_plain_assertion_failure(buggy_repo):
    """这道闸最要紧的边界：**真断言失败的栈同样只到测试文件**。

    被调函数正常返回了，栈上没有它——所以「栈没穿进产品代码」这一条单独用
    会把每一条正常的复现测试都打回去。类型判定因此不能省。

    与上面那条 `accepts_a_genuine_assertion_failure` 不重复：那条钉的是
    「断言失败算复现」，这条钉的是「第 4 道闸不误伤它」，删掉任何一条都会有
    一整类回归无人看守。
    """
    (buggy_repo / "tests" / "test_repro.py").write_text(
        "from calc import add\n\n\ndef test_x():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    ok, reason = await red_check(buggy_repo, PytestAdapter(),
                                 "tests/test_repro.py::test_x")
    assert ok is True, reason


def test_typo_reason_names_the_actual_missing_name():
    """**成因不能是一句套话。**

    实测（2026-08-04，ai-learning-helper#89）：缺的是 `UrlBlockStore`，而回帖
    照样写「最常见的成因：用了 `pytest.raises` 却没有 `import pytest`」——
    照着那句话去查，会去骂模型没写 `import pytest`，而真正的原因是 test_code
    不自包含（它指着仓库里已有的测试文件写了个片段，撞名改名后名字全失效）。

    这个项目把「指错方向的诊断」看得比崩溃还重，这里就是一处现行。
    """
    from pathlib import Path

    from aifix.adapters.base import Failure, SourceCandidate
    from aifix.reproduce import _typo_reason

    class _Adapter:
        def locate_source(self, failure, repo):
            return [SourceCandidate(path="tests/t.py", line=6, frame="test_x",
                                    origin="traceback")]

    f = Failure(test_id="tests/t.py::test_x", classname="tests.t", name="test_x",
                message="NameError: name 'UrlBlockStore' is not defined",
                trace="", file="tests/t.py")
    reason = _typo_reason(f, Path("."), _Adapter())

    assert reason, "栈只到测试文件的 NameError 仍该被挡下"
    assert "UrlBlockStore" in reason, "必须点出缺的是哪个名字"
    assert "import pytest" not in reason, (
        "缺的不是 pytest，却把成因说成 import pytest —— 指错方向的诊断")


def test_typo_guard_passes_when_it_cannot_see_any_frame():
    """没有栈帧时**放行**——「没有证据」不能当成「有罪的证据」。

    否则这道闸会在自己瞎掉的时候变得最严厉，而那正是最不该拦人的时候。
    直接调 `_typo_reason` 而不真跑 pytest：要构造的正是「解析不出帧」这个
    异常态，真跑造不出来。
    """
    from pathlib import Path

    from aifix.adapters.base import Failure
    from aifix.reproduce import _typo_reason

    class _Blind:
        def locate_source(self, failure, repo):
            return []

    f = Failure(test_id="t.py::x", classname="t", name="x",
                message="NameError: name 'pytest' is not defined",
                trace="", file="t.py")
    assert _typo_reason(f, Path("."), _Blind()) == ""


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
    out = await reproduce(buggy_repo, [PytestAdapter()], cfg,
                          "add 算错了", "add(2,3) 返回 -1，期望 5",
                          client=_Scripted([_text(_OK_JSON)]))
    assert out.reproduction is not None
    assert out.reproduction.target_test_id == "tests/test_issue_1.py::test_sum"
    assert out.tokens > 0


async def test_reproduce_reports_missing_info_as_the_reason(buggy_repo):
    """信息不足是一条**结论**，不是错误 —— 它要原样回帖给人看。"""
    raw = json.dumps({"can_reproduce": False,
                      "missing_info": ["没说触发的输入", "没说期望输出"]})
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "有 bug", "反正就是不对",
                          client=_Scripted([_text(raw)]))
    assert out.reproduction is not None
    assert out.reproduction.can_reproduce is False
    assert "没说触发的输入" in out.reason


async def test_reproduce_degrades_to_none_on_unparseable_output(buggy_repo):
    """解析不了不抛异常：上层据此走「写不出复现」通路，回帖说明。"""
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text("我觉得吧……")]))
    assert out.reproduction is None
    assert out.reason


# ---------------------------------------------------------------- 失败分类

async def test_giving_up_is_labelled_as_missing_info(buggy_repo):
    """信息不足 —— 下一步是**人**去补充 issue。"""
    raw = json.dumps({"can_reproduce": False, "missing_info": ["没说触发的输入"]})
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text(raw)]))
    assert out.kind == "missing_info"


async def test_unparseable_output_is_labelled_as_such(buggy_repo):
    """输出不合格式 —— 下一步是**运维**去看 trace / 换模型，不是补 issue。"""
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text("我觉得吧……")]))
    assert out.kind == "unparseable"


async def test_running_out_of_steps_is_not_confused_with_missing_info(buggy_repo):
    """步数耗尽 ≠ issue 信息不足。**这两条的下一步动作完全相反**：

    前者要人去补 issue，后者要运维去调步数上限或换模型。归并成一句「没能写出
    复现测试」，用户会去改 issue —— 改多少遍都没用。

    这是 2026-07-30 第一次真跑（issue #1）撞出来的：模型翻了 25 步没作答，
    回帖说的却是「没能写出复现测试」。
    """
    cfg = AifixConfig(reproducer_max_steps=1)
    # 永远只调工具、从不作答的模型
    looping = _Scripted([_tool_call("read_file", '{"path": "calc.py"}')])
    out = await reproduce(buggy_repo, [PytestAdapter()], cfg,
                          "t", "b", client=looping)
    assert out.kind == "no_convergence", out.reason
    assert out.reproduction is None
    # 消息里要出现**可操作**的那个旋钮名 —— 环境变量形式，不是字段名：
    # 读这条消息的人要去设的是环境变量，给他字段名等于让他自己去翻源码
    assert "AIFIX_REPRODUCER_MAX_STEPS" in out.reason


async def test_the_events_are_carried_out_for_tracing(buggy_repo):
    """复现这一步的事件必须带出来落盘。

    第一次真跑时它整段没有 trace（RunTrace 建在 run_once 里，而这条通路根本
    走不到那儿）—— 于是「模型这 25 步在读什么」这个唯一有诊断价值的问题，
    artifact 里一个字都没有。失败时恰恰最需要它。
    """
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text("随便")]))
    assert out.events, "事件流是空的，出问题时无从复盘"


async def test_a_token_overrun_is_also_no_convergence(buggy_repo):
    """token 超限与步数耗尽是**同一类**：循环没跑完、没有最终文本可解析。

    第三次真跑（2026-07-30）里它被报成「模型的输出解析不出复现测试」——
    一句指向输出格式的话，而真相是额度不够。当时的判据是 `"max_steps" in err`
    这种字符串挑选，挑不中就落进解析类；改成按**结构**分：ok 为 False 就是
    没收敛，与错误文本长什么样无关（那是框架的措辞，随时会变）。
    """
    # 必须让它进入**第二步**：预算检查发生在下一步开始之前，单轮就结束的桩
    # 触发不到（这一点是写这条测试时才发现的）。
    cfg = AifixConfig(reproducer_max_tokens=1)
    out = await reproduce(
        buggy_repo, [PytestAdapter()], cfg, "t", "b",
        client=_Scripted([_tool_call("read_file", '{"path": "calc.py"}')]))
    assert out.kind == "no_convergence", out.reason
    assert "解析" not in out.reason


async def test_reproduce_cannot_eat_the_whole_budget(buggy_repo, monkeypatch):
    """复现最多花掉整份预算的一部分，剩下的留给修复。

    实测（2026-07-30，issue #2，当时的币种是美元）：pro 跑 25 步把整份预算
    全吃光，run_once 拿到 0 当场中止，报告写「预算耗尽：0 / 0」—— 一句看不出
    是被前一步吃光的话。
    """
    seen = {}
    real = __import__("aifix.agents.runner", fromlist=["consume"]).consume

    async def _spy(stream, cost_cap=None, **k):
        seen["cap"] = cost_cap
        return await real(stream, cost_cap=cost_cap, **k)

    monkeypatch.setattr("aifix.reproduce.consume", _spy)
    cfg = AifixConfig(budget_cny=0.50, reproducer_budget_share=0.4)
    await reproduce(buggy_repo, [PytestAdapter()], cfg, "t", "b",
                    client=_Scripted([_text("x")]))
    assert seen["cap"] == pytest.approx(0.20)


async def test_no_cost_gate_when_no_budget_is_set(buggy_repo, monkeypatch):
    """budget_cny 为 0 是「不设闸」，不是「额度已扣光」。

    传 0.0 进去会让 consume 把**第一次**调用就掐掉 —— 恰好在闸本不该存在的
    时候把它关死（与 fix_node 里 `0.0 or None` 那处同款）。
    """
    seen = {}

    async def _spy(stream, cost_cap=None, **k):
        seen["cap"] = cost_cap
        from aifix.agents.runner import consume as real
        return await real(stream, cost_cap=cost_cap, **k)

    monkeypatch.setattr("aifix.reproduce.consume", _spy)
    await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(budget_cny=0.0),
                    "t", "b", client=_Scripted([_text("x")]))
    assert seen["cap"] is None


async def test_an_empty_answer_is_told_apart_from_a_malformed_one(buggy_repo):
    """一个字都没吐 ≠ 吐了但格式不对。

    实测（2026-07-30，issue #2）：pro 把输出预算全烧在 reasoning_content 里，
    正文一个字都没有 —— 事件流里 ReasoningDelta 1001 条、TextDelta 0 条，
    第 9 步没有 StepFinished，最后一条 ModelUsage 的 token 数是 None（被截断）。

    归成「输出不合约定的 JSON 格式」是错的：那句话让人去看它吐了什么，
    而它什么都没吐。下一步该是压推理长度或换模型。
    """
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text("")]))
    assert out.kind == "empty_answer", out.reason
    assert "推理" in out.reason


async def test_a_malformed_answer_is_still_called_that(buggy_repo):
    """反向对照：真吐了东西但解析不出时，措辞不能被上一条吃掉。"""
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "t", "b", client=_Scripted([_text("我觉得吧……")]))
    assert out.kind == "unparseable"
    assert "推理" not in out.reason


def test_thinking_is_off_by_default():
    """默认关。

    这是个**取舍决定**，不是测量结论：实测见过一轮推理吃光输出预算导致零产出，
    也见过一轮带推理写得很好。真要下结论需要开/关各跑一批任务比修复成功率，
    那个读数还不存在 —— 所以这条断言钉的是「默认值是什么」，不是「哪个更好」。
    """
    from aifix.reproduce import _route
    assert AifixConfig().reproducer_thinking is False
    assert _route(AifixConfig()).llm_extra_body["enable_thinking"] is False


def test_thinking_can_be_restored_to_the_endpoint_default():
    """None = 不发这个参数。这条出路必须留着：默认关是取舍，不是定论，
    而各家端点的默认并不相同。"""
    from aifix.reproduce import _route
    cfg = AifixConfig(reproducer_thinking=None)
    assert _route(cfg) is cfg.fixer
    assert "enable_thinking" not in _route(cfg).llm_extra_body


def test_thinking_can_be_turned_off_for_this_step_only():
    """关掉时只动复现这一步的路由，fixer 自己那份不受影响 —— 两步的活不同，
    fixer 要看测试反馈迭代，推理对它可能真有用。"""
    from aifix.reproduce import _route
    cfg = AifixConfig(reproducer_thinking=False)
    assert _route(cfg).llm_extra_body["enable_thinking"] is False
    assert "enable_thinking" not in cfg.fixer.llm_extra_body


def test_an_empty_env_value_does_not_break_startup():
    """Actions 里 `env: X: ${{ vars.Y }}` 在 Y 未设置时给的是**空串**。

    不接这一手，任何没配这个 variable 的仓库都会在启动时因为「'' 不是合法的
    bool」而拒绝启动 —— 一个纯粹由 YAML 语义造成的、与用户无关的崩溃。
    """
    assert AifixConfig(reproducer_thinking="").reproducer_thinking is None
    # 而没设这个变量时走代码里的默认（关），不是 None
    assert AifixConfig().reproducer_thinking is False


def test_null_restores_the_endpoint_default():
    """`--body null` 要能表达「不发这个参数」—— 空串已经被 workflow 的
    `|| 'false'` 占用了，得留另一个出口。"""
    for v in ("null", "none", "NULL", ""):
        assert AifixConfig(reproducer_thinking=v).reproducer_thinking is None


async def test_a_stream_cut_mid_step_is_not_blamed_on_the_model(buggy_repo):
    """流在某一步中途断掉 ≠ 模型输出格式不对。

    实测（2026-07-30，issue #2，两轮）：事件流里 StepStarted 比 StepFinished
    多一条，正文停在「Let me look at the existing test to confirm...」，**一个
    花括号都没有** —— 模型还在干活，是流断了。而 consume() 的 async-for 正常
    退出，outcome.ok 是 True，于是被归成「输出不合约定的 JSON 格式」。

    那句话让人去改 prompt 或换模型，而真相是这次调用没跑完 —— 下一步该是重试
    或查端点。这是**静默截断**：不报错、不崩溃，只有诊断是假的。
    """
    from harness.events import RunFinished, StepFinished, StepStarted

    from aifix.reproduce import classify_incomplete

    # 有 RunFinished = 正常收场
    assert classify_incomplete([StepStarted(step=1), RunFinished(message=None)]) is False
    # 没有 = 流断了
    assert classify_incomplete([StepStarted(step=1)]) is True

    # **反向对照，这条最要紧**：正常收场的最后一步本来就不发 StepFinished，
    # 所以「StepStarted 比 StepFinished 多」在每一次成功里都成立 —— 拿它当判据
    # 会把所有成功判成截断。这条弯路真走过一次。
    normal = [StepStarted(step=1), StepFinished(step=1),
              StepStarted(step=2), RunFinished(message=None)]
    assert classify_incomplete(normal) is False


async def test_hitting_our_own_cost_gate_says_so(buggy_repo):
    """撞上**我们自己**的成本闸，不能报成「端点掐流」。

    实测（2026-07-30，issue #2，当时的币种是美元）两轮的累计成本是 0.2179 /
    0.2070，而闸是 0.50 × 0.4 = 0.20 —— `consume` 主动关掉生成器，事件流里
    没有 RunFinished，
    签名与「端点断流」一模一样。上一版据此报「重试，或查端点是不是在长响应上
    掐流」，那是一句假话：端点没问题，是我们自己掐的。

    `consume` 早就把这件事记在 `cost_capped` 上了 —— 只是没人读。
    """
    # 必须配价格表：不配的话成本恒为 0、闸永远不触发 —— 这正是这个项目
    # 反复强调的那件事，写测试时先自己踩了一遍。
    cfg = AifixConfig(fixer={"model": "m"}, price_map={"m": [1.0, 1.0]},
                      budget_cny=0.001, reproducer_budget_share=0.4)
    out = await reproduce(buggy_repo, [PytestAdapter()], cfg, "t", "b",
                          client=_Scripted([_text("x"), _text("y")]))
    assert out.kind == "cost_capped", out.reason
    # 消息里要给出**可操作**的旋钮。
    # 断言的是「有没有让人去查端点」，不是「出没出现端点这两个字」——
    # 消息里那句「也不是端点的问题」是**否定句**，按关键词判会把它误伤，
    # 而它恰恰是这条消息最该说的话。
    assert "AIFIX_BUDGET_CNY" in out.reason
    assert "AIFIX_REPRODUCER_BUDGET_SHARE" in out.reason
    assert "查端点" not in out.reason and "重试" not in out.reason


async def test_the_reproduce_step_carries_its_event_timestamps(buggy_repo):
    """复现这一步的时刻也要一路带到 ReproduceOutcome 上。

    **这条是被一次真实的缺口逼出来的**：`event_times` 加进 AgentOutcome 之后，
    ReproduceOutcome 上忘了加，而 handle 那边写的是 `getattr(out, ..., None)`
    —— 于是它「跑通了」，只是复现那一段一个时间戳都没有。而复现恰恰是这条
    流水线里最该计时的几段之一（实测一次真跑 44,577 tokens、好几分钟）。

    防御性的 getattr 正好掩盖了这个缺口，所以那一处也一并改成了直接访问。
    """
    out = await reproduce(buggy_repo, [PytestAdapter()], AifixConfig(),
                          "add 算错了", "add(2,3) 返回 -1",
                          client=_Scripted([_text(_OK_JSON)]))
    assert out.event_times, "复现这一步一个时间戳都没有"
    assert len(out.event_times) == len(out.events), \
        "时刻与事件必须一一对应 —— 错位一格，replay 会把耗时算到别的步上"


# ------------------------------- 调用失败 ≠ 没在预算内收敛

def test_a_transport_error_is_not_reported_as_non_convergence():
    """**实测逼出来的**（2026-08-04，百炼专属网关）。

    一次服务端 `AgentServiceGetResultError` 被报成「模型没能在预算内收敛」，
    下一步写的是「调大 AIFIX_REPRODUCER_MAX_STEPS / MAX_TOKENS」—— 对一个服务端
    错误，调多少步都没用。指错方向的诊断。

    判据按**我们自己的上限**算，不按错误文本挑：错误文本是框架和端点的，随时会
    变（上一版拿 `"max_steps" in err` 挑就是这么栽的）。真撞上限时步数或 token
    必然贴到配置值；没贴到就说明是半路断的。
    """
    from aifix.reproduce import KIND_CALL_FAILED, KIND_NO_CONVERGENCE

    assert KIND_CALL_FAILED != KIND_NO_CONVERGENCE

    from aifix.config import AifixConfig
    from aifix.reproduce import hit_ceiling, steps_used

    cfg = AifixConfig(reproducer_max_steps=25, reproducer_max_tokens=250_000)

    # 半路断的：步数与 token 都远没贴到上限 → 不是没收敛，是调用挂了
    assert hit_ceiling(3, 4_000, cfg) is False
    # 真翻满了
    assert hit_ceiling(25, 4_000, cfg) is True
    assert hit_ceiling(3, 250_000, cfg) is True


def test_steps_are_counted_by_event_type_not_by_error_text():
    """判据按**我们自己的配置**算，不按错误文本挑 —— 错误文本是框架和端点的，
    随时会变。上一版拿子串挑，挑不中的全落进另一档，于是 token 超限被报成
    「输出格式不对」。"""
    from aifix.reproduce import steps_used

    class _Step:
        pass
    _Step.__name__ = "StepStarted"

    class _Other:
        pass

    assert steps_used([_Step(), _Other(), _Step()]) == 2
    assert steps_used([]) == 0


def test_the_new_kind_has_its_own_headline():
    """kind 只是个标签，真正给人看的是回帖开头那句。少了映射的话，新增的这一档
    会以一句空标题出现 —— 而它存在的全部理由就是「话说得对不对」。"""
    from aifix.issue.handle import _REPRO_HEADLINES
    from aifix.reproduce import KIND_CALL_FAILED

    assert KIND_CALL_FAILED in _REPRO_HEADLINES
    assert "调用" in _REPRO_HEADLINES[KIND_CALL_FAILED]
