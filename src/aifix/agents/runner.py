"""把 AgentLoop 的异步事件流收敛成一个供 LangGraph 节点使用的结果对象。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from harness.events import (Event, ModelUsage, RunError, TextDelta,
                            ToolFinished, ToolStarted)
from harness.types import ToolCall, ToolResult


@dataclass
class AgentOutcome:
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    events: list[Event] = field(default_factory=list)
    # 每条事件**到达时**的 epoch 秒，与 `events` 一一对应、同序同长。
    #
    # 为什么是两个平行列表而不是包一层带时刻的对象：`events` 会被
    # `violations.count_violations` 与 `reproduce.classify_incomplete` 按
    # `isinstance` / 类名消费 —— 包一层就要改那两处，而它们判的是**框架的
    # 事件类型**，不该被一个观测需求侵入。代价是两个列表必须同序，所以全
    # 项目只在 `consume` 的一个位置往里 append（见下面那处注释）。
    event_times: list[float] = field(default_factory=list)
    # 成本上限触发，事件流被我们**主动**截断。刻意不复用 error：两者说的是
    # 两件事 —— error 来自 RunError，含义是「这次 run 自己跑坏了」；截断是
    # 我们从外面按预算掐的，被掐的那次 run 本身是健康的，它已经产出的东西
    # （落地的补丁、已吐出的文本）全都仍然有效。混进 error 会让 ok 变成
    # False，于是这些成果被当作失败一并丢掉，报告也再分不出「被钱掐断」
    # 与「跑崩了」——而这两种情况该给用户的下一步动作完全不同。
    #
    # 注：目前只有 fix_node 传 cost_cap，detect_node 不传，所以 detect 这条
    # 路径永远不会被截断。字段仍然保留，它是 fix_node 判「要不要继续守卫
    # 重试」的依据（见 fix.py 里对 outcome.cost_capped 的处理）。
    cost_capped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


async def consume(
        stream: AsyncIterator[Event],
        cost_cap: float | None = None,
        on_tool: Callable[[ToolCall], None] | None = None,
        on_tool_done: Callable[[ToolCall, ToolResult], None] | None = None,
) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用。

    cost_cap：累计成本越过该值即停止消费并关闭生成器。

    on_tool / on_tool_done：工具调用的开始与结束各回调一次，给进度显示用。
    两个都要，因为它们答的是两个问题：

    - `on_tool` 听 `ToolStarted`，答「它现在在干什么」。fix 是一次 run 里最长
      的一段（默认最多 25 步），只在结束时出声的话，最需要心跳的那几分钟仍然
      是空屏 —— 而 run_tests 一跑就是几十秒。
    - `on_tool_done` 听 `ToolFinished`，答「成了还是砸了、为什么砸」。这个只有
      结果事件知道。实跑过的一次 run：23 次调用里 5 次是错的（诊断指了个不存在
      的文件、补丁 check 不过、跑了失败列表外的用例），不听结束事件的话，屏幕
      上它们和成功的长得一模一样。

    结束回调**带上发起时的那次调用**：参数只在 `ToolStarted` 上，`ToolFinished`
    只有一个 tool_call_id，光凭它渲染出来的行只剩一个工具名。没见过开始的
    结束事件直接跳过（不该发生，但宁可少报一条也不凭空造一条）。

    听的是 `ToolStarted`，**不是 `ToolCall`** —— 后者是消息里的数据结构，
    从不作为事件出现在流上。这一条踩过：单元测试自己构造 ToolCall 喂进来，
    测试全绿而真跑一步都不报。实测一次真 run 的 events.jsonl：
    ToolStarted 33 条、ToolFinished 33 条、ToolCallRequested 30 条，
    ToolCall **0 条**。

    契约是「越线之后不再发起新的模型调用」，不是「绝不超过一分钱」——
    成本只有在调用返回后才知道（ModelUsage 到达的那一刻），所以越线时
    那一次调用必然已经花掉。超支上界因此是可陈述的：一次模型调用。
    """
    parts: list[str] = []
    out = AgentOutcome()
    # 发起过的调用，供结束事件按 id 认领自己的参数
    calls: dict[str, ToolCall] = {}
    async for ev in stream:
        # **时刻在这里记，不在落盘那一侧。** record_events 是整段循环跑完之后
        # 批量写的，在那里打戳会让全部事件挤在同一毫秒上 —— 看着精确，而据此
        # 算出来的每步耗时全是 0。这两行必须挨着，它们的同序性没有别的保障。
        out.events.append(ev)
        out.event_times.append(time.time())
        if isinstance(ev, ToolStarted):
            calls[ev.tool_call.id] = ev.tool_call
            if on_tool is not None:
                on_tool(ev.tool_call)
        elif isinstance(ev, ToolFinished):
            call = calls.pop(ev.result.tool_call_id, None)
            if on_tool_done is not None and call is not None:
                on_tool_done(call, ev.result)
        elif isinstance(ev, TextDelta):
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
        # 关掉传进来的生成器。**这只关掉了一层壳，不要把它当成清理完成。**
        #
        # 框架的 AgentLoop.run() 本身只是个壳：
        #     async for ev in self._run_from(state, resuming=False):
        #         yield ev
        # 真正的 ExitStack（负责还原「打转纠偏」时调高的采样温度）和
        # run / step / model_call 三个 OpenTelemetry span 全都在 _run_from
        # 里。aclose() 把 GeneratorExit 抛进壳的 yield 处，而 `async for`
        # 不会顺手关闭它遍历的那个内层异步生成器 —— _run_from 就那样挂在
        # 原地，要等事件循环做异步生成器收尾（后续某次 await 时的 GC 回收，
        # 最迟到 asyncio.run() 的 shutdown_asyncgens()）才被终结。
        #
        # 也就是说：**升温泄漏给下一次调用的隐患仍然完整存在**，还原时机
        # 依旧不确定，aclose() 只是让它看起来被处理过了。跑成本闸的测试时
        # 打出来的那两段 `Failed to detach context` 堆栈就是这件事的收据 ——
        # 它们是 span 在与创建时不同的上下文里 detach 才报的，栈底正指着
        # _run_from 里 `yield ModelUsage(...)` 那一行的 GeneratorExit。
        # 不要给 opentelemetry.context 装 filter 把它消音：调高日志级别试过，
        # 无效（报错不在那个窗口内发生），而装长期 filter 等于撕掉收据、
        # 隐患照旧。
        #
        # 真修需要给框架的 run() / resume() 各包一层 contextlib.aclosing，
        # 那要发版并回归另一个项目，超出本里程碑范围。
        #
        # getattr 是为了兼容不提供 aclose 的迭代器。
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            await closer()
    out.text = "".join(parts)
    return out
