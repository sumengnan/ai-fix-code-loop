"""把 AgentLoop 的异步事件流收敛成一个供 LangGraph 节点使用的结果对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from harness.events import (Event, ModelUsage, RunError, TextDelta,
                            ToolStarted)


@dataclass
class AgentOutcome:
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    events: list[Event] = field(default_factory=list)
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


async def consume(stream: AsyncIterator[Event],
                  cost_cap: float | None = None,
                  on_tool: Callable[[str], None] | None = None) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用。

    cost_cap：累计成本越过该值即停止消费并关闭生成器。

    on_tool：每次**工具调用**回调一次，参数是工具名。给进度显示用 —— fix 是
    一次 run 里最长的一段（默认最多 25 步，每步可能读码、搜索、打补丁、跑
    目标用例），只在它结束后出声的话，最需要心跳的那几分钟仍然是空屏。
    听的是 `ToolStarted`，**不是 `ToolCall`** —— 后者是消息里的数据结构，
    从不作为事件出现在流上。这一条踩过：单元测试自己构造 ToolCall 喂进来，
    测试全绿而真跑一步都不报。实测一次真 run 的 events.jsonl：
    ToolStarted 33 条、ToolFinished 33 条、ToolCallRequested 30 条，
    ToolCall **0 条**。听 ToolFinished 也不对 —— 那是工具跑完才报，而心跳
    的意义正是在工具**跑着的时候**让人知道它在跑（run_tests 一跑几十秒）。

    契约是「越线之后不再发起新的模型调用」，不是「绝不超过一分钱」——
    成本只有在调用返回后才知道（ModelUsage 到达的那一刻），所以越线时
    那一次调用必然已经花掉。超支上界因此是可陈述的：一次模型调用。
    """
    parts: list[str] = []
    out = AgentOutcome()
    async for ev in stream:
        out.events.append(ev)
        if isinstance(ev, ToolStarted):
            if on_tool is not None:
                on_tool(ev.tool_call.name)
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
