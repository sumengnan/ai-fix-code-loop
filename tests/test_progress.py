"""跑到一半的时候，用户看得见什么。

`aifix run` 此前从头到尾一个字都不印：`_cmd_run` 只在 `asyncio.run` 返回
**之后**打一次报告。而一次真跑是 5 分钟起步（baseline 一次全量、每个 failure
两次模型调用、每轮 verify 再一次全量），中间那几分钟屏幕上是空的 —— 分不出
「在干活」和「卡死了」。

同一个文件里 `mine` / `mutate` / `eval` 三条命令早就有进度回调了，只有 `run`
没接。这一组测试钉的就是接上之后的契约。
"""
from __future__ import annotations

import io

from aifix.progress import NullProgress, TerminalProgress


def _term() -> tuple[TerminalProgress, io.StringIO]:
    buf = io.StringIO()
    return TerminalProgress(stream=buf), buf


class _Tty(io.StringIO):
    """假装自己是终端的流 —— 颜色与就地重写只在这种流上发生。"""

    def isatty(self) -> bool:
        return True


def _tty() -> tuple[TerminalProgress, _Tty]:
    buf = _Tty()
    return TerminalProgress(stream=buf), buf


def _plain(text: str) -> str:
    """剥掉 ANSI 转义，只留可见字符。"""
    import re
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", text)


def test_the_default_progress_says_nothing():
    """默认必须是哑的。

    `run_once` 不只被 CLI 调用 —— `eval` 会**并行**跑几十个任务，每个都是一次
    完整的 run。默认出声的话，几十条进度会交织成一团，而 eval 自己的
    `on_done` 那一行反而被淹掉。库调用方（未来的服务）同理。
    """
    p = NullProgress()
    p.run_start(run_id="r1", adapter="pytest", branch="aifix/r1")
    p.baseline(ran=14, failing=2, seconds=0.4)
    p.failure_start(index=1, total=2, test_id="t.py::x")
    p.verified(verdict="better", seconds=1.0)
    p.finished(fixed=1, total=2, tokens=100, cny=0.0)
    # 什么都不该发生；这里没有可断言的输出，能跑通就是契约本身
    assert isinstance(p, NullProgress)


def test_progress_goes_to_stderr_not_stdout(capsys):
    """报告走 stdout，进度走 stderr —— 这条决定 `aifix run . > report.md` 好不好用。

    混在 stdout 里的话，重定向出来的 report.md 顶上会粘着几十行进度，
    而那正是最常见的用法（把报告存下来、贴进 PR）。
    """
    p = TerminalProgress()
    p.baseline(ran=14, failing=2, seconds=0.4)
    out, err = capsys.readouterr()
    assert out == "", f"进度污染了 stdout：{out!r}"
    assert "14" in err and "2" in err


def test_baseline_line_reports_scale_and_cost():
    """baseline 那一行要同时给出「跑了多少」和「红了多少」。

    只给红的数目分不出「2 个红」是 2/14 还是 2/2000 —— 后者意味着接下来每轮
    verify 都要再跑一次那 2000 个，用户看到的停顿会长得多，而那是正常的。
    """
    p, buf = _term()
    p.baseline(ran=1783, failing=3, seconds=466.0)
    line = buf.getvalue()
    assert "1783" in line and "3" in line
    assert "466" in line or "7:46" in line, f"没给耗时：{line!r}"


def test_failure_header_shows_position_in_the_queue():
    """[2/7] 这种位置信息是「还要等多久」的唯一线索。"""
    p, buf = _term()
    p.failure_start(index=2, total=7, test_id="tests/test_cart.py::test_满减")
    line = buf.getvalue()
    assert "2/7" in line
    assert "tests/test_cart.py::test_满减" in line


def test_agent_steps_are_a_heartbeat_during_the_long_phase():
    """fix 是最长的一段，必须有心跳。

    每一步印工具名：模型在读文件、在打补丁、还是在跑测试，是判断「它有没有
    在做有用的事」的最低限度信息。没有这一条，最长的那几分钟仍然是空屏。
    """
    p, buf = _tty()
    p.agent_step(step=3, tool="apply_patch", arguments={})
    p.agent_step_done(step=3, tool="apply_patch", ok=True, arguments={},
                      result="补丁已应用。当前改动：\n（无）")
    p.agent_step(step=4, tool="run_tests", arguments={})
    p.agent_step_done(step=4, tool="run_tests", ok=True, arguments={},
                      result="exit_code=0\nstdout:\n1 passed in 0.03s\n")
    out = buf.getvalue()
    assert "apply_patch" in out and "run_tests" in out
    assert "3" in out and "4" in out


# ---------- 工具调用：成败、内容、原因 ----------

def test_a_tool_that_worked_gets_a_check_in_front_of_its_name():
    """打勾在**工具名前面**，一列对齐 —— 扫一眼就知道哪几步栽了。

    标记跟在名字后面的话，工具名长短不一，勾和叉会散落在各个列上，
    「哪一步失败了」这个唯一重要的问题又得逐行读。
    """
    p, buf = _term()
    p.agent_step_done(step=6, tool="read_file", ok=True,
                      arguments={"path": "src/shopcart/cart.py"},
                      result="a\nb\nc\n")
    line = buf.getvalue()
    assert "✓" in line and "✗" not in line
    assert line.index("✓") < line.index("read_file"), "标记必须在工具名前面"
    assert "src/shopcart/cart.py" in line, "要说清它对什么东西做了这件事"


def test_a_tool_that_failed_gets_a_cross_and_says_why():
    """失败必须给出原因 —— 这是这条改动存在的理由。

    实跑过的一次 run：23 次工具调用里 5 次是错的（诊断指了个不存在的文件、
    补丁 check 不过、跑了失败列表外的用例），终端一条都没标出来，成功和
    失败长得一模一样。要翻 events.jsonl 才知道模型在原地打转。
    """
    p, buf = _term()
    p.agent_step_done(
        step=2, tool="read_file", ok=False,
        arguments={"path": "src/cart.py"},
        result=("工具执行出错: [Errno 2] No such file or directory: "
                "'/Users/x/PycharmProjects/demo/.aifix/runs/ab/tree/src/cart.py'"))
    line = buf.getvalue()
    assert "✗" in line and "✓" not in line
    assert line.index("✗") < line.index("read_file")
    assert "src/cart.py" in line
    assert "No such file" in line, "原因要落到屏幕上"
    assert "工具执行出错" not in line, "这个前缀是噪音，标记已经说了它是错的"


def test_a_red_test_is_not_a_failed_tool():
    """`run_tests` 报告用例红了，是它**成功**跑完的结果。

    把 exit_code=1 当成工具失败的话，屏幕上会出现一片红叉，而模型正在
    正常工作 —— 真正的失败（守卫拒绝、补丁打不上）就淹没在里面了。
    """
    p, buf = _term()
    p.agent_step_done(
        step=9, tool="run_tests", ok=True,
        arguments={"test_ids": ["tests/test_cart.py::test_排行_按单品总价降序"]},
        result="exit_code=1\nstdout:\nF   [100%]\n1 failed in 0.04s\n")
    line = buf.getvalue()
    assert "✓" in line, "工具本身跑成功了"
    assert "1 failed" in line, "但用例是红的，这句必须在"


def test_each_tool_says_what_it_actually_did():
    """内容取自参数与结果，一行一个工具，不用去翻 events.jsonl。"""
    cases = [
        (dict(tool="list_files", ok=True, arguments={"path": "src"},
              result="shopcart"), ["src", "1"]),
        # path 有默认值，模型不传是常态：印成 `language=python`（沙箱挑运行时
        # 用的提示）的话，看起来像它在列一个叫 language 的东西
        (dict(tool="list_files", ok=True, arguments={"language": "python"},
              result="a\nb"), [".", "2"]),
        (dict(tool="grep", ok=True, arguments={"pattern": "most_expensive"},
              result="src/a.py:1:x\nsrc/b.py:2:y"), ["most_expensive", "2"]),
        (dict(tool="apply_patch", ok=True,
              arguments={"diff": "--- a/src/shopcart/cart.py\n"
                                 "+++ b/src/shopcart/cart.py\n@@ -1 +1 @@\n"},
              result="补丁已应用。当前改动：\nsrc/shopcart/cart.py | 2 +-\n"
                     " 1 file changed, 1 insertion(+), 1 deletion(-)"),
         ["src/shopcart/cart.py", "+1", "-1"]),
        (dict(tool="run_tests", ok=False,
              arguments={"test_ids": ["a::x", "a::y", "a::z"]},
              result="未知的测试标识：['a::y', 'a::z']。只能跑当前失败列表中的用例"),
         ["3", "未知的测试标识"]),
    ]
    for kwargs, expected in cases:
        p, buf = _term()
        p.agent_step_done(step=1, **kwargs)
        line = buf.getvalue()
        for want in expected:
            assert want in line, f"{kwargs['tool']} 少了 {want!r}：{line!r}"


def test_the_marks_are_colored_on_a_tty_and_plain_in_a_file():
    """颜色只给终端。

    进度走 stderr，而 `2> run.log` 是真实用法；ANSI 码写进文件就是一串
    `ESC[32m` 垃圾，grep 出来的行也对不上。
    """
    p, tty = _tty()
    p.agent_step_done(step=1, tool="read_file", ok=True,
                      arguments={"path": "a.py"}, result="x")
    p.agent_step_done(step=2, tool="read_file", ok=False,
                      arguments={"path": "b.py"}, result="炸了")
    out = tty.getvalue()
    assert "\033[32m" in out, "对钩是绿的"
    assert "\033[31m" in out, "叉号是红的"

    p, buf = _term()
    p.agent_step_done(step=1, tool="read_file", ok=True,
                      arguments={"path": "a.py"}, result="x")
    line = buf.getvalue()
    assert "\033[" not in line, f"文件里不该有 ANSI：{line!r}"
    assert "✓" in line, "标记本身仍然要在"


def test_the_heartbeat_speaks_before_the_result_is_known():
    """勾还是叉要等工具跑完才知道，但那段等待不能是空屏。

    `run_tests` 一跑几十秒。终端上先印「进行中」的半行，跑完就地重写成
    带标记的那一行 —— 屏幕上最终只留一行，等待期间却不是空的。
    """
    p, tty = _tty()
    p.agent_step(step=7, tool="run_tests",
                 arguments={"test_ids": ["tests/test_cart.py::test_满减"]})
    mid = tty.getvalue()
    assert "run_tests" in mid, "工具还没跑完就该出声"
    assert not mid.endswith("\n"), "这半行待会儿要被重写掉，不能先换行"

    p.agent_step_done(step=7, tool="run_tests", ok=True,
                      arguments={"test_ids": ["tests/test_cart.py::test_满减"]},
                      result="exit_code=0\nstdout:\n1 passed in 0.03s\n")
    out = tty.getvalue()
    assert "\r" in out, "就地重写，不是追加一行"
    assert out.count("\n") == 1, f"一次工具调用最终只占一行：{out!r}"
    assert "✓" in out and "1 passed" in out


def test_off_a_tty_a_tool_call_is_exactly_one_line():
    """重定向到文件时没人盯着看，那半行只会变成噪音。

    而且 `\\r` 重写在文件里是不生效的：进行中那半行会原样留下，一次 run
    的日志里凭空多出二十几行残句。
    """
    p, buf = _term()
    p.agent_step(step=7, tool="run_tests", arguments={"test_ids": ["a::b"]})
    assert buf.getvalue() == "", "非终端上，开始时不出声"

    p.agent_step_done(step=7, tool="run_tests", ok=True,
                      arguments={"test_ids": ["a::b"]},
                      result="exit_code=0\nstdout:\n1 passed in 0.03s\n")
    out = buf.getvalue()
    assert out.count("\n") == 1 and "\r" not in out
    assert "✓" in out


def test_no_output_line_is_ever_wide_enough_to_wrap():
    """每一行都必须放得下。

    折行会把就地重写打回原形（`\\r` 只回到最后一个视觉行的行首），屏幕上
    留下一串对不齐的残句。中文按两格宽算 —— 按字符数截会漏掉一倍宽度。
    """
    from aifix.progress import _line_budget, _width
    p, buf = _term()
    p.agent_step_done(step=1, tool="grep", ok=False,
                      arguments={"pattern": "x" * 200},
                      result="搜索失败：" + "很长的中文原因" * 40)
    for line in buf.getvalue().splitlines():
        assert _width(line) <= _line_budget(), f"宽度超了：{line!r}"


def test_a_failure_reason_is_not_thrown_away_to_fit_one_line():
    """失败原因**不截断**，放不下就换行接着写。

    这条是被真事逼出来的：`✗ apply_patch → 补丁无法应用（git apply --check
    失败）：error:…` —— 省略号后面正是 `corrupt patch at line 10`，也就是这次
    失败的全部信息量。前缀那句样板话把预算吃光，真正要看的那半句被挤掉了，
    只能回头去翻 events.jsonl。

    成功那行截掉无所谓（`87 行` 少几个字不影响判断），失败这行截掉就等于
    没印。
    """
    p, buf = _term()
    p.agent_step_done(
        step=12, tool="apply_patch", ok=False,
        arguments={"diff": "--- a/src/shopcart/cart.py\n"
                           "+++ b/src/shopcart/cart.py\n"},
        result=("补丁无法应用（git apply --check 失败）："
                "error: corrupt patch at line 10\n"
                "这是 diff 的格式问题，不是文件内容问题。"))
    out = buf.getvalue()
    assert "corrupt patch at line 10" in out, "真因不能被省略号吃掉"
    assert "格式问题" in out, "后续几行也是原因的一部分"
    assert out.count("\n") > 1, "放不下就换行，不是截断"
    # 续行要缩进对齐，否则读起来像另一个步骤
    assert all(ln.startswith("      ") for ln in out.splitlines())


def test_a_successful_step_stays_on_one_line():
    """成功仍是一行 —— 一次 run 二十几个工具调用，每个占三行就没法看了。"""
    p, buf = _term()
    p.agent_step_done(step=1, tool="read_file", ok=True,
                      arguments={"path": "a/" * 60 + "x.py"},
                      result="x\n" * 500)
    assert buf.getvalue().count("\n") == 1


def test_an_enormous_failure_reason_still_has_a_ceiling():
    """再长也不能刷屏。

    某些 git / pytest 的报错能吐几百行，全印出来会把前面几步顶出屏幕 ——
    那时省略号是诚实的：完整正文一直在 events.jsonl 里。
    """
    p, buf = _term()
    p.agent_step_done(step=1, tool="run_tests", ok=False,
                      arguments={"test_ids": ["a::b"]},
                      result="炸了。" + "很长的中文原因，" * 300)
    out = buf.getvalue()
    assert out.count("\n") <= 6, f"太长了：{out.count(chr(10))} 行"
    assert "…" in out, "截断要看得出来是截断"


def test_verdict_line_uses_the_same_words_as_the_report():
    """判定的措辞与报告一致 —— 两处各说各的会让人以为是两回事。

    报告里 better/same/worse 印的是「已修复 / 未修复 / 变差」（report._VERDICT_CN）。
    """
    from aifix.nodes.report import _VERDICT_CN
    for verdict, word in _VERDICT_CN.items():
        p, buf = _term()
        p.verified(verdict=verdict, seconds=1.0)
        assert word in buf.getvalue(), f"{verdict} 的措辞与报告对不上"


def test_notes_survive_even_when_nothing_else_is_printed():
    """守卫触发、中止原因这类事必须出声。

    它们是「为什么这一轮花了钱却没进展」的解释，只写进 facts.jsonl 的话，
    用户要等 run 结束、再去 replay 才看得到。
    """
    p, buf = _term()
    p.note("空 diff 守卫触发，重试")
    assert "空 diff 守卫触发，重试" in buf.getvalue()


def test_every_line_is_flushed():
    """不 flush 的话进度会被缓冲到 run 结束才一次吐出 —— 等于没做。

    stderr 默认行缓冲，但重定向到文件时会变成块缓冲（4KB 起），而一次 run
    的进度远不到 4KB。`mine` / `eval` 那几处 print 都显式带了 flush=True。
    """
    class _Recorder(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushes = 0

        def flush(self):
            self.flushes += 1

    rec = _Recorder()
    p = TerminalProgress(stream=rec)
    p.baseline(ran=1, failing=1, seconds=0.1)
    p.note("x")
    assert rec.flushes >= 2, "每条进度都要 flush"


# ---------- 接线：最长的那一段有没有心跳 ----------

async def test_consume_reports_every_tool_call():
    """`consume` 要能把工具调用逐条报出来。

    fix 是一次 run 里最长的一段（模型读码、搜索、打补丁、跑目标用例，
    默认最多 25 步）。只在它结束后出声的话，最需要心跳的那几分钟仍然是空屏。
    """
    from harness.events import ToolCall, ToolFinished, ToolResult, ToolStarted
    from aifix.agents.runner import consume

    def _started(name):
        return ToolStarted(tool_call=ToolCall(id="1", name=name, arguments={}))

    async def _stream():
        yield _started("read_file")
        yield ToolFinished(result=ToolResult(tool_call_id="1", content="ok"))
        yield _started("apply_patch")

    seen: list[str] = []

    async def _gen():
        async for e in _stream():
            yield e

    await consume(_gen(), on_tool=lambda call: seen.append(call.name))
    assert seen == ["read_file", "apply_patch"], \
        "每次工具调用报一条，结果事件不重复报"


async def test_consume_reports_the_outcome_too():
    """开始与结束各报一次：勾还是叉，只有 `ToolFinished` 知道。

    结束回调必须拿得到**发起时的那次调用**（参数在 `ToolStarted` 上，
    `ToolFinished` 只有 tool_call_id）—— 否则屏幕上只剩一个工具名，
    「对哪个文件失败了」还是得去翻 events.jsonl。
    """
    from harness.events import ToolCall, ToolFinished, ToolResult, ToolStarted
    from aifix.agents.runner import consume

    async def _gen():
        yield ToolStarted(tool_call=ToolCall(id="c1", name="read_file",
                                             arguments={"path": "src/cart.py"}))
        yield ToolFinished(result=ToolResult(tool_call_id="c1", content="没有这个文件",
                                             is_error=True))

    done: list[tuple] = []
    await consume(_gen(),
                  on_tool_done=lambda call, res: done.append(
                      (call.name, call.arguments, res.is_error, res.content)))
    assert done == [("read_file", {"path": "src/cart.py"}, True, "没有这个文件")]


async def test_a_result_without_a_start_is_not_reported():
    """没见过开始的结束事件不报。

    步号是在开始时发出去的；对着一个从没编过号的调用去报结束，屏幕上会
    冒出一个与上下文对不上的步号。宁可少报一条。
    """
    from harness.events import ToolFinished, ToolResult
    from aifix.agents.runner import consume

    async def _gen():
        yield ToolFinished(result=ToolResult(tool_call_id="ghost", content="x"))

    done = []
    await consume(_gen(), on_tool_done=lambda c, r: done.append(c))
    assert done == []


def test_the_result_line_reuses_the_step_number_of_its_own_call():
    """一个 step 可以并发发起多条工具调用 —— 结束回调不能用自增计数器编号。

    实跑过的第 1 步就同时发了两条 read_file。用「结束一次加一」来编号的话，
    先返回的那条会顶掉另一条的号；`[✓ 第 1 步] [✗ 第 2 步]` 里谁是谁全错。
    按 tool_call_id 认领才对。
    """
    from harness.types import ToolCall, ToolResult
    from aifix.progress import StepReporter

    class _Rec(NullProgress):
        def __init__(self):
            self.lines = []

        def agent_step(self, step, tool, arguments):
            self.lines.append(("start", step, tool))

        def agent_step_done(self, step, tool, ok, arguments, result):
            self.lines.append(("done", step, tool, ok))

    rec = _Rec()
    r = StepReporter(rec)
    a = ToolCall(id="a", name="read_file", arguments={"path": "t.py"})
    b = ToolCall(id="b", name="read_file", arguments={"path": "s.py"})
    r.started(a)
    r.started(b)
    # 后发的先回
    r.finished(b, ToolResult(tool_call_id="b", content="炸了", is_error=True))
    r.finished(a, ToolResult(tool_call_id="a", content="ok"))

    assert rec.lines == [("start", 1, "read_file"), ("start", 2, "read_file"),
                         ("done", 2, "read_file", False),
                         ("done", 1, "read_file", True)]


def test_step_numbers_keep_counting_across_guard_retries():
    """步号在整个 failure 内累加 —— 守卫重试是同一次修复尝试的延续。

    每轮从 1 重数会让屏幕上出现两段「第 1 步」，读起来像是重新开始了。
    一个 `StepReporter` 跨多次 `consume` 复用，这条契约就是它的存在理由。
    """
    from harness.types import ToolCall, ToolResult
    from aifix.progress import StepReporter

    seen = []

    class _Rec(NullProgress):
        def agent_step_done(self, step, tool, ok, arguments, result):
            seen.append(step)

    r = StepReporter(_Rec())
    for i in range(3):
        call = ToolCall(id=f"c{i}", name="read_file", arguments={})
        r.started(call)
        r.finished(call, ToolResult(tool_call_id=f"c{i}", content="ok"))
    assert seen == [1, 2, 3]


async def test_consume_without_a_callback_is_unchanged():
    """不传回调时行为一个字节不变 —— detect 与所有既有调用点都走这条路。"""
    from harness.events import ToolCall, ToolStarted
    from aifix.agents.runner import consume

    async def _gen():
        yield ToolStarted(tool_call=ToolCall(id="1", name="read_file",
                                             arguments={}))

    out = await consume(_gen())
    assert out.ok and len(out.events) == 1
