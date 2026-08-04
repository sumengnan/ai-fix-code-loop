from harness.events import (
    ModelUsage, RunError, RunFinished, RunStarted, TextDelta,
)
from harness.types import Message, Role
from harness.usage import Usage

from aifix.agents.runner import AgentOutcome, consume
from aifix.runtime.money import CNY, Money

# 价表已经是人民币的规则：折算因子为 1，事件里的数就是最终的数。
# 下面绝大多数用例要钉的是**累加与截断**，不是汇率，用它把汇率变量摘出去。
_ONE = Money(price_currency=CNY)


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
    ]), money=_ONE)
    assert out.text == "前后"
    assert out.tokens == 15
    assert out.cost_cny == 0.02
    assert out.error is None
    assert out.ok is True


async def test_captures_error():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunError(error="达到 max_steps 上限 (25)"),
    ]), money=_ONE)
    assert out.error == "达到 max_steps 上限 (25)"
    assert out.ok is False


async def test_accumulates_usage_across_steps():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.01,
                   attempts=1, latency_ms=1.0, model="m"),
        ModelUsage(usage=Usage(20, 10, 30), cost_usd=0.03,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]), money=_ONE)
    assert out.tokens == 45
    assert abs(out.cost_cny - 0.04) < 1e-9


async def test_none_cost_treated_as_zero():
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=None,
                   attempts=1, latency_ms=1.0, model="m"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]), money=_ONE)
    assert out.cost_cny == 0.0


async def test_events_are_retained_for_tracing():
    out = await consume(_stream([
        RunStarted(run_id="r"),
        RunFinished(message=Message(role=Role.ASSISTANT, content="")),
    ]), money=_ONE)
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

    out = await consume(stream(), cost_cap=0.5, money=_ONE)
    assert out.cost_capped is True
    assert abs(out.cost_cny - 0.8) < 1e-9, "第二次 usage 越线，第三次不该发生"
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

    out = await consume(stream(), cost_cap=0.5, money=_ONE)
    assert abs(out.cost_cny - 0.98) < 1e-9      # 0.49 未越线，0.98 越线即停
    assert out.cost_capped is True


async def test_no_cap_consumes_everything():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=5.0,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="尾巴")

    out = await consume(stream(), money=_ONE)
    assert out.cost_capped is False
    assert out.text == "尾巴"


async def test_cap_not_reached_leaves_flag_false():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.1,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="没超")

    out = await consume(stream(), cost_cap=0.5, money=_ONE)
    assert out.cost_capped is False
    assert abs(out.cost_cny - 0.1) < 1e-9
    assert out.text == "没超"


async def test_cost_cap_keeps_text_already_received():
    """熔断不该丢掉已经拿到手的输出 —— detect 可能已经吐完 JSON 了。"""
    async def stream():
        yield TextDelta(text='{"suspect_file": "a.py"}')
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=9.9,
                          attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="后面的")

    out = await consume(stream(), cost_cap=0.5, money=_ONE)
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

    out = await consume(slow(), money=_ONE)
    assert len(out.event_times) == len(out.events) == 2
    assert out.event_times[1] - out.event_times[0] >= 0.04, out.event_times


async def test_every_event_gets_exactly_one_timestamp():
    """两个列表必须一一对应 —— 错位一格，replay 会把某一步的耗时算到
    另一步头上，而那种错是看不出来的。"""
    out = await consume(_stream([
        RunStarted(run_id="r"), TextDelta(text="x"), TextDelta(text="y"),
    ]), money=_ONE)
    assert len(out.event_times) == len(out.events) == 3


async def test_usd_price_map_is_converted_to_cny_here_and_only_here():
    """折算就发生在这一处（见 money.py）。

    钉的是「事件里的数是价表货币、AgentOutcome 里的数是人民币」这条边界。
    往下游任何一层再折一次，得到的都是一个看着完全正常的 7 倍。
    """
    out = await consume(_stream([
        ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.02,
                   attempts=1, latency_ms=1.0, model="m"),
    ]), money=Money(usd_to_cny=7.2))
    assert abs(out.cost_cny - 0.144) < 1e-9


async def test_cost_cap_is_compared_in_cny_not_price_currency():
    """闸的单位必须与累加值一致。

    一边人民币一边美元的话，¥1 的闸要等价表算出 $1 才拦 —— 也就是 ¥7.2，
    7 倍于用户设的上限，而这道闸一路上看起来都在正常工作。
    """
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.2,
                         attempts=1, latency_ms=1.0, model="m")
        yield TextDelta(text="不该被消费")

    out = await consume(stream(), cost_cap=1.0, money=Money(usd_to_cny=7.2))
    assert out.cost_capped is True, "$0.2 折成 ¥1.44 已经越过 ¥1 的闸"
    assert out.text == ""
