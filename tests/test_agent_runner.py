from harness.events import (
    ModelUsage, RunError, RunFinished, RunStarted, TextDelta,
)
from harness.types import Message, Role
from harness.usage import Usage

from aifix.agents.runner import AgentOutcome, consume


async def _stream(events):
    for e in events:
        yield e


async def test_collects_text_and_usage():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        TextDelta(text="前"),
        TextDelta(text="后"),
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.02,
                   attempts=1, latency_ms=12.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="前后")),
    ]))
    assert out.text == "前后"
    assert out.tokens == 15
    assert out.cost_usd == 0.02
    assert out.error is None
    assert out.ok is True


async def test_captures_error():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunError(error="达到 max_steps 上限 (25)"),
    ]))
    assert out.error == "达到 max_steps 上限 (25)"
    assert out.ok is False


async def test_accumulates_usage_across_steps():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.01,
                   attempts=1, latency_ms=1.0, model="m"),
        ModelUsage(usage=Usage(20, 10, 30), cost_usd=0.03,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert out.tokens == 45
    assert abs(out.cost_usd - 0.04) < 1e-9


async def test_none_cost_treated_as_zero():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=None,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert out.cost_usd == 0.0


async def test_events_are_retained_for_tracing():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]))
    assert len(out.events) == 2
    assert isinstance(out.events[0], RunStarted)


async def test_cost_cap_stops_consuming():
    """越线后停止消费 —— 这是整个成本闸的地基。"""
    closed = []

    async def stream():
        try:
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4,
                              attempts=1, latency_ms=1.0, model="m")
            yield TextDelta(text="第一段")
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4,
                              attempts=1, latency_ms=1.0, model="m")
            yield TextDelta(text="第二段")
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4,
                              attempts=1, latency_ms=1.0, model="m")
            yield TextDelta(text="第三段")
        finally:
            closed.append(True)

    out = await consume(stream(), cost_cap=0.5)
    assert out.cost_capped is True
    assert abs(out.cost_usd - 0.8) < 1e-9, "第二次 usage 越线，第三次不该发生"
    assert out.text == "第一段", "越线后的文本不该再被消费"
    assert closed == [True], "必须显式 aclose，不能把生成器留给 GC"


async def test_cost_cap_overshoot_is_one_model_call():
    """契约：越线后不再发起新调用，而不是「不超过一分钱」。

    断言的是「多花了正好一次调用」——直接断言 cost <= cap 会是假的，
    而写一个假的断言比不写更糟。
    """
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49,
                          attempts=1, latency_ms=1.0, model="m")
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49,
                          attempts=1, latency_ms=1.0, model="m")
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49,
                          attempts=1, latency_ms=1.0, model="m")

    out = await consume(stream(), cost_cap=0.5)
    assert abs(out.cost_usd - 0.98) < 1e-9      # 0.49 未越线，0.98 越线即停
    assert out.cost_capped is True


async def test_no_cap_consumes_everything():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=5.0,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="尾巴")

    out = await consume(stream())
    assert out.cost_capped is False
    assert out.text == "尾巴"


async def test_cap_not_reached_leaves_flag_false():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.1,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="没超")

    out = await consume(stream(), cost_cap=0.5)
    assert out.cost_capped is False
    assert abs(out.cost_usd - 0.1) < 1e-9
    assert out.text == "没超"


async def test_cost_cap_keeps_text_already_received():
    """熔断不该丢掉已经拿到手的输出 —— detect 可能已经吐完 JSON 了。"""
    async def stream():
        yield TextDelta(text='{"suspect_file": "a.py"}')
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=9.9,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="后面的")

    out = await consume(stream(), cost_cap=0.5)
    assert out.text == '{"suspect_file": "a.py"}'
    assert out.error is None, "主动截断不是运行出错，不能污染 error"


async def test_events_are_stamped_as_they_arrive_not_at_the_end():
    """时刻要在**事件到达那一刻**记，不是整段跑完之后补。

    record_events 是批量落盘的 —— 在那里打戳，全部事件会挤在同一毫秒上，
    算出来的「每步耗时」全是 0。这条测试用一个会真的停顿的流来钉住：
    两条事件之间隔了实打实的时间，戳就必须差出来。
    """
    import asyncio

    async def slow():
        yield TextDelta(text="a")
        await asyncio.sleep(0.05)
        yield TextDelta(text="b")

    out = await consume(slow())
    assert len(out.event_times) == len(out.events) == 2
    assert out.event_times[1] - out.event_times[0] >= 0.04, out.event_times


async def test_every_event_gets_exactly_one_timestamp():
    """两个列表必须一一对应 —— 错位一格，replay 会把某一步的耗时算到
    另一步头上，而那种错是看不出来的。"""
    out = await consume(_stream([
        RunStarted(run_id="r"), TextDelta(text="x"), TextDelta(text="y"),
    ]))
    assert len(out.event_times) == len(out.events) == 3
