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
    # 成本上限触发，事件流被我们**主动**截断。刻意不复用 error：
    # detect_node 用 outcome.ok 决定要不要解析诊断，而模型很可能在被截断前
    # 就已经吐完了 JSON —— 把主动截断混进 error 会白白丢掉一个可用的诊断。
    cost_capped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


async def consume(stream: AsyncIterator[Event],
                  cost_cap: float | None = None) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用。

    cost_cap：累计成本越过该值即停止消费并关闭生成器。

    契约是「越线之后不再发起新的模型调用」，不是「绝不超过一分钱」——
    成本只有在调用返回后才知道（ModelUsage 到达的那一刻），所以越线时
    那一次调用必然已经花掉。超支上界因此是可陈述的：一次模型调用。
    """
    parts: list[str] = []
    out = AgentOutcome()
    async for ev in stream:
        out.events.append(ev)
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ModelUsage):
            out.tokens += ev.usage.total_tokens
            out.cost_usd += ev.cost_usd or 0.0
            if cost_cap is not None and out.cost_usd >= cost_cap:
                out.cost_capped = True
                break
        elif isinstance(ev, RunError):
            out.error = ev.error
    if out.cost_capped:
        # 必须显式关闭：只 break 会把异步生成器留给 GC，而框架在其中用
        # ExitStack 还原「打转纠偏」的采样升温 —— 清理时机不确定，就可能
        # 把升温漏给下一次调用。getattr 是为了兼容不提供 aclose 的迭代器。
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            await closer()
    out.text = "".join(parts)
    return out
