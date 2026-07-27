"""把 AgentLoop 的异步事件流收敛成一个供 LangGraph 节点使用的结果对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from harness.events import Event, ModelUsage, RunError, TextDelta


@dataclass
class AgentOutcome:
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    events: list[Event] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


async def consume(stream: AsyncIterator[Event]) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用（M2 落 events.jsonl）。"""
    parts: list[str] = []
    out = AgentOutcome()
    async for ev in stream:
        out.events.append(ev)
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ModelUsage):
            out.tokens += ev.usage.total_tokens
            out.cost_usd += ev.cost_usd or 0.0
        elif isinstance(ev, RunError):
            out.error = ev.error
    out.text = "".join(parts)
    return out
