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
