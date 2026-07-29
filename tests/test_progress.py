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
    p.finished(fixed=1, total=2, tokens=100, usd=0.0)
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
    p, buf = _term()
    p.agent_step(step=3, tool="apply_patch")
    p.agent_step(step=4, tool="run_tests")
    out = buf.getvalue()
    assert "apply_patch" in out and "run_tests" in out
    assert "3" in out and "4" in out


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

    await consume(_gen(), on_tool=lambda n: seen.append(n))
    assert seen == ["read_file", "apply_patch"], \
        "每次工具调用报一条，结果事件不重复报"


async def test_consume_without_a_callback_is_unchanged():
    """不传回调时行为一个字节不变 —— detect 与所有既有调用点都走这条路。"""
    from harness.events import ToolCall, ToolStarted
    from aifix.agents.runner import consume

    async def _gen():
        yield ToolStarted(tool_call=ToolCall(id="1", name="read_file",
                                             arguments={}))

    out = await consume(_gen())
    assert out.ok and len(out.events) == 1
