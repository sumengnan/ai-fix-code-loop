"""把一段缺陷报告变成一条**红着的**复现测试。

这一步在核心循环之外、之前：产出的测试先 commit 进 HEAD，随后 `run_once`
从 HEAD 建 worktree，baseline 自然把它认成一个失败用例，`only_test` 把队列
削成只有它——于是核心循环一行都不用改。

**这里没有 LangGraph 节点。** 它不在图里，也就不该放进 nodes/：图的入口是
`run_once`，而复现必须发生在 `run_once` 之前（测试要先进 HEAD）。放在 nodes/
会让人以为它是 build_graph() 装配的一环。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.context.manager import ContextManager
from harness.llm.openai_compat import OpenAICompatibleClient
from harness.loop.agent_loop import AgentLoop
from harness.reliability.budget import BudgetTracker
from harness.sandbox.base import Sandbox
from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolRegistry
from harness.tools.builtins.fs_tools import ListFilesTool, ReadFileTool

from .adapters.base import ProjectAdapter
from .agents.reproducer import (SYSTEM_PROMPT, Reproduction, build_prompt,
                                parse_reproduction)
from .agents.runner import consume
from .config import AifixConfig
from .nodes.baseline import file_level_ids, run_scoped
from .tools.search import GrepTool


@dataclass
class ReproduceOutcome:
    """`reproduction is None` 表示这一步没能产出任何可用结论。

    与「模型如实说信息不足」不是一回事：后者 `reproduction.can_reproduce`
    为 False 且 `missing_info` 非空，是一条**有价值的 triage 结论**，要原样
    回帖。两者共用一个 None 会让「模型答歪了」和「issue 写得不全」在报告里
    长得一模一样，而这两种情况该给人的下一步动作完全不同。
    """
    reproduction: Reproduction | None
    reason: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    events: list[Any] = field(default_factory=list)


def build_reproduce_registry(sandbox: Sandbox,
                             adapter: ProjectAdapter) -> ToolRegistry:
    """reproducer 的能力面：**只读**，三个工具。

    没有 apply_patch：复现测试由确定性代码写下去，不经过工具面——这正是
    「不许改测试文件」那道守卫不用为 M6 改一行的原因。给了 apply_patch，
    它就能直接改产品代码，而那条路径上一道守卫都没有。

    没有 run_tests：让它自己跑测试，「这条测试红不红」的判定权就落到了模型
    手里。红检是这一步唯一的确定性证据，不能交出去。

    adapter 目前只用于保持与 build_registry 一致的签名；红检那边才真正用到它。
    """
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    return reg


def write_reproduction(worktree: Path, r: Reproduction) -> Path:
    """把测试写进工作区，返回落盘路径。

    建父目录：模型完全可能给出 `tests/regression/test_x.py`，而那个子目录
    未必存在。路径安全（相对、无 `..`、落在 test_dirs 之下）已在
    parse_reproduction 里校验过，这里不重复判——重复判两份会各自漂移。
    """
    p = Path(worktree) / r.test_file
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r.test_code or "", encoding="utf-8")
    return p


async def red_check(worktree: Path, adapter: ProjectAdapter,
                    target_id: str) -> tuple[bool, str]:
    """复现测试必须**红**，而且要红得有信息量。零 LLM。

    三种「不算复现」分开报，因为它们指向完全不同的下一步：

    1. **收集错误**——import 不到东西也是红，但它复现的是模型自己的笔误。
       新功能的测试红在 ImportError 上是常态（函数还不存在）；修 bug 不是，
       产品代码就在那儿。放过这一类，fixer 会被派去修一个不存在的模块。
    2. **用例没跑出结果**——node id 不存在、被跳过、收集没走到它。
    3. **跑了但没失败**——这条测试在当前代码上就是绿的，约束力为零。

    判定顺序不能换：收集失败时 target 必然也「没跑出结果」，先判后者会给出
    一句指错方向的话（本项目最贵的失败一向不是崩溃，是指错方向的诊断）。
    """
    fs = await run_scoped(worktree, adapter, [target_id])

    stuck = file_level_ids(sorted(fs.ids), adapter)
    if stuck:
        return False, (
            f"复现测试在**收集**阶段就失败了（{'、'.join(stuck)}）。\n"
            "  这种红说明 import 或语法有问题，复现的是笔误而不是缺陷——"
            "产品代码是现成的，import 不到多半是模块路径猜错了。")

    if target_id not in fs.ran:
        return False, (
            f"`{target_id}` 没有跑出任何结果。\n"
            "  可能是 node id 与实际写下去的用例名对不上，或者它被跳过了。")

    if target_id not in fs.ids:
        return False, (
            f"`{target_id}` 在当前代码上**没有失败**。\n"
            "  一条现在就绿的测试对修复没有任何约束力——它要么没抓住报告里"
            "描述的行为，要么那个缺陷已经被修过了。")

    return True, ""


async def reproduce(worktree: Path, adapter: ProjectAdapter,
                    config: AifixConfig, issue_title: str, issue_body: str,
                    client: Any = None) -> ReproduceOutcome:
    """带只读工具的一次 AgentLoop，产出一条复现测试的源码。

    **一次成型，不重试。** fix 那边的守卫重试是有确定反馈可喂回去的（diff
    空了、越界了）；这里失败的形态多半是「测试红得不对」，把红检的理由喂回去
    再来一轮值不值，得先有数据。v1 一次定生死，正是为了让任务 3 的验收给出
    一个干净的读数——带重试的成功率量不出模型一次能做对多少。

    模型路由复用 `fixer`：写复现测试要读代码、拼对 import 和调用签名，量级
    接近 fixer 而不是 detector（后者是单步、无工具、强制 JSON 的诊断）。
    什么时候该拆出第三条路由——等实测发现便宜模型也够用的时候。

    不套 `json_output()`：那会强制整轮输出 JSON，而这一轮里模型要先调工具。
    容错交给 parse_reproduction 的围栏剥离（与 parse_diagnosis 同款）。
    """
    sandbox = LocalSandbox(workspace=str(worktree))
    await sandbox.start()
    try:
        loop = AgentLoop(
            client=client or OpenAICompatibleClient(config.fixer),
            registry=build_reproduce_registry(sandbox, adapter),
            context=ContextManager(SYSTEM_PROMPT),
            max_steps=config.fixer_max_steps,
            budget=BudgetTracker(max_tokens=config.budget_tokens,
                                 max_wall_seconds=config.budget_wall_seconds),
            loop_detect_window=config.loop_detect_window,
            tool_result_max_chars=config.tool_result_max_chars,
            model_name=config.fixer.model,
            price_map=config.price_map,
        )
        prompt = build_prompt(issue_title, issue_body, adapter.test_dirs())
        outcome = await consume(loop.run(prompt))
    finally:
        await sandbox.close()

    common = dict(tokens=outcome.tokens, cost_usd=outcome.cost_usd,
                  events=outcome.events)
    if not outcome.ok:
        return ReproduceOutcome(
            None, f"生成复现测试时出错：{outcome.error}", **common)

    r = parse_reproduction(outcome.text, adapter.test_dirs())
    if r is None:
        return ReproduceOutcome(
            None, "模型没有给出可解析的复现测试（输出不合约定的 JSON 格式）。",
            **common)
    if not r.can_reproduce:
        return ReproduceOutcome(
            r, "issue 里的信息不足以写出复现测试，还缺：\n"
            + "\n".join(f"  - {m}" for m in r.missing_info), **common)
    return ReproduceOutcome(r, **common)
