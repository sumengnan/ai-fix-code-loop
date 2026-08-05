"""第三层：跑一遍，证伪或确认。

语义与复现那一步**完全一致**，所以整条链原样复用：

    对比测试**红** —— 两处对同一输入给了不同答案 = 缺陷
    绿            —— 两处一致，候选作废

于是 `red_check` 的四道闸白捡：红在收集错误上、红在自己的笔误上、根本没跑
起来 —— 这几种「红得没有信息量」在这里同样是「这次探测不算数」。

`agreed` **不是失败**，是这一层最常见的结果。把它和「探测出错」混成一个
返回值，人就分不清「查过了，很干净」与「压根没查成」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.base import ProjectAdapter
from ..agents.reproducer import Reproduction
from ..agents.twin_prober import SYSTEM_PROMPT, build_prompt, parse_probe
from ..config import AifixConfig
from ..nodes.baseline import run_scoped
from ..reproduce import (_route, build_reproduce_registry, red_check,
                         write_reproduction)
from .twins import Twin

# 确认了不一致：产出一条红着的测试，可以直接进修复循环。
KIND_OK = "ok"
# 两边一致。候选作废 —— 这是好消息，不是错误。
KIND_AGREED = "agreed"
# 模型说这两个压根不是一回事（第一层是启发式的，会配错）。
KIND_NOT_COMPARABLE = "not_comparable"
# 对比测试自己有问题：红在收集错误 / 自己的笔误上，或根本没跑起来。
KIND_BAD_PROBE = "bad_probe"
# 输出不合约定格式，或模型没作答。
KIND_UNPARSEABLE = "unparseable"


@dataclass
class ProbeOutcome:
    twin: Twin
    kind: str
    reason: str = ""
    # 只有 KIND_OK 时非空 —— 它就是修复循环要的那条复现测试。
    reproduction: Reproduction | None = None
    tokens: int = 0
    cost_cny: float = 0.0
    events: list[Any] = field(default_factory=list)


async def probe_twin(worktree: Path, adapter: ProjectAdapter, twin: Twin,
                     config: AifixConfig | None = None,
                     client: Any = None) -> ProbeOutcome:
    """给一对候选写对比测试并跑一遍。

    `client` 注入时不建真客户端 —— 与别处同一条理由：调用方已经决定了模型是
    什么（评测的替身、测试的脚本），探一个替身证明不了任何事。
    """
    from harness.context.manager import ContextManager
    from harness.llm.openai_compat import OpenAICompatibleClient, json_output
    from harness.loop.agent_loop import AgentLoop
    from harness.reliability.budget import BudgetTracker
    from harness.sandbox.local import LocalSandbox

    from ..agents.runner import consume

    cfg = config or AifixConfig()
    sandbox = LocalSandbox(workspace=str(worktree))
    await sandbox.start()
    try:
        loop = AgentLoop(
            client=client or OpenAICompatibleClient(_route(cfg)),
            registry=build_reproduce_registry(sandbox, adapter),
            context=ContextManager(SYSTEM_PROMPT),
            max_steps=cfg.reproducer_max_steps,
            budget=BudgetTracker(max_tokens=cfg.reproducer_max_tokens),
            model_name=cfg.detector.model,
            price_map=cfg.price_map,
        )
        with json_output():
            outcome = await consume(
                loop.run(build_prompt(twin, list(adapter.test_dirs()))),
                money=cfg.money)
    finally:
        await sandbox.close()

    common = {"tokens": outcome.tokens, "cost_cny": outcome.cost_cny,
              "events": list(outcome.events)}

    if not outcome.ok or not (outcome.text or "").strip():
        return ProbeOutcome(twin, KIND_UNPARSEABLE,
                            "模型没有作答，或调用本身失败了。", **common)

    r, why = parse_probe(outcome.text, adapter.is_test_path)
    if r is None:
        return ProbeOutcome(twin, KIND_UNPARSEABLE, why, **common)
    if not r.can_reproduce:
        return ProbeOutcome(twin, KIND_NOT_COMPARABLE,
                            "；".join(r.missing_info), **common)

    path = write_reproduction(worktree, r)
    keep = False
    try:
        # 先问「它到底失败了没有」，再决定要不要走红检。
        #
        # 两件事必须分开：**测试绿 = 两边一致**，那是这一层最常见、也是好的
        # 结果；而红检报出来的是「红得没有信息量」。合成一个返回值的话，
        # 「查过了，很干净」会和「压根没查成」长得一模一样。
        fs = await run_scoped(worktree, [adapter], [r.target_test_id],
                              timeout=cfg.scoped_test_timeout_seconds)
        if r.target_test_id in fs.ran and r.target_test_id not in fs.ids:
            return ProbeOutcome(twin, KIND_AGREED,
                                "两处对这份输入给出了相同结果，候选作废。",
                                **common)

        ok, reason = await red_check(worktree, adapter, r.target_test_id,
                                     timeout=cfg.scoped_test_timeout_seconds)
        if not ok:
            return ProbeOutcome(twin, KIND_BAD_PROBE, reason, **common)

        # 只有确认了不一致才把文件留下 —— 它是修复循环的输入。
        keep = True
        return ProbeOutcome(twin, KIND_OK,
                            "两处对同一份输入给出了不同结果。",
                            reproduction=r, **common)
    finally:
        # 作废的候选**不留痕**：留在工作区里会被下一次 baseline 收进去，
        # 于是一条「探测过、结论是没问题」的测试变成了一个工单。
        if not keep:
            path.unlink(missing_ok=True)
