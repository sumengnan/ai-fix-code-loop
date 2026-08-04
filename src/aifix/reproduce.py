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
from harness.tools.builtins.fs_tools import ListFilesTool

from .adapters.base import Failure, ProjectAdapter
from .agents.reproducer import (SYSTEM_PROMPT, Reproduction, build_prompt,
                                parse_reproduction_ex)
from .agents.runner import consume
from .config import AifixConfig
from .nodes.baseline import file_level_ids, run_scoped
from .tools.read import ReadFileTool
from .tools.read_symbol import ReadSymbolTool
from .tools.search import GrepTool


# 这一步的四种收场。**分开是因为下一步动作完全不同**，归并成一句「没能写出
# 复现测试」会把人指向错的方向：
#
#   ok             —— 有了一条可用的复现测试
#   missing_info   —— issue 信息不足。下一步是**人**去补充 issue
#   no_convergence —— 模型翻了一堆文件就是不作答。下一步是**运维**调
#                     reproducer_max_steps 或换模型；让人去改 issue 改多少遍都没用
#   unparseable    —— 输出不合约定格式。同样是运维侧的事（看 trace / 换模型）
#   empty_answer   —— 正文一个字都没有。推理型模型会把输出预算全烧在
#                     reasoning_content 里然后被截断，正文永远等不到
#   truncated      —— 流在某一步中途断了，这次调用没跑完。**静默**：consume 的
#                     async-for 正常退出、ok 为 True，看起来像模型答歪了
#   cost_capped    —— 撞上我们自己的美元闸。事件签名与 truncated 一模一样，
#                     必须先判它，否则会报成「端点在掐流」——一句假话
#
# 这四类是实测逼出来的：2026-07-30 第一次真跑（issue #1）沿用 fixer 的 25 步，
# 模型翻了 25 步没吐 JSON，而回帖说的是「没能写出复现测试」—— 一句会让人去
# 改 issue 的话，而改 issue 根本不解决问题。
KIND_OK = "ok"
KIND_MISSING_INFO = "missing_info"
KIND_NO_CONVERGENCE = "no_convergence"
KIND_UNPARSEABLE = "unparseable"
# 一个字都没吐。与 unparseable 分开，因为下一步完全不同：那一类要去看它吐了
# 什么，这一类根本没有可看的东西。实测（2026-07-30，issue #2）见下方注释。
KIND_EMPTY_ANSWER = "empty_answer"
# 流在某一步中途断了。与 unparseable 分开：那一类是模型答歪了，这一类是这次
# 调用**根本没跑完** —— 下一步是重试或查端点，不是改 prompt。
KIND_TRUNCATED = "truncated"
# 模型调用**本身**失败了（端点报错、网络断、凭据不对），而不是模型答不好。
# 与 no_convergence 分开：那一档的下一步是「调大上限 / 换模型」，对一个服务端
# 错误完全无效 —— 实测（2026-08-04）一次 AgentServiceGetResultError 就是被那样
# 报的。判据是**有没有撞上我们自己的上限**（见 reproduce 里那段）。
KIND_CALL_FAILED = "call_failed"
# 撞上**我们自己**的美元闸。与 truncated 的事件签名一模一样（都没有
# RunFinished），但原因和下一步完全不同 —— 那一类要查端点，这一类要调预算。
KIND_COST_CAPPED = "cost_capped"


@dataclass
class ReproduceOutcome:
    """`reproduction is None` 表示这一步没能产出任何可用结论。

    与「模型如实说信息不足」不是一回事：后者 `reproduction.can_reproduce`
    为 False 且 `missing_info` 非空，是一条**有价值的 triage 结论**，要原样
    回帖。两者共用一个 None 会让「模型答歪了」和「issue 写得不全」在报告里
    长得一模一样，而这两种情况该给人的下一步动作完全不同。

    `kind` 是给**程序**判的（handle 据此选回帖措辞），`reason` 是给人看的。
    """
    reproduction: Reproduction | None
    reason: str = ""
    kind: str = KIND_OK
    tokens: int = 0
    cost_usd: float = 0.0
    events: list[Any] = field(default_factory=list)
    # 与 `events` 一一对应的到达时刻（见 agents.runner.AgentOutcome）。
    # **必须一路带过来**：复现是这条流水线里最长的几段之一（实测一次真跑
    # 44,577 tokens、好几分钟），漏掉它 replay 就正好在最该计时的地方没有时间。
    event_times: list[float] = field(default_factory=list)


def classify_incomplete(events: list[Any]) -> bool:
    """这次调用有没有正常收场 —— 判据是**有没有 `RunFinished`**。

    实测（2026-07-30）正常收场的事件序列是
    `RunStarted → StepStarted → TextDelta → ModelUsage → RunFinished`：
    **最后一步不发 `StepFinished`**，发的是 `RunFinished`。所以「StepStarted 比
    StepFinished 多」在**每一次**正常收场里都成立，拿它当判据会把所有成功都
    判成截断 —— 这条弯路走过一次，留在这里。

    而 issue #2 那两轮失败的事件统计里**一条 `RunFinished` 都没有**：流在中途
    断了，`consume()` 的 async-for 却正常退出、`outcome.ok` 为 True —— 从返回值
    上完全看不出来。这是一次**静默截断**：不报错、不崩溃，只有「模型输出格式
    不对」这个诊断是假的。

    只在 `outcome.ok` 为真的分支里调用：有 `RunError` 时已经归进 no_convergence。
    """
    return not any(type(e).__name__ == "RunFinished" for e in events)


def steps_used(events: list[Any]) -> int:
    """这一轮实际走了几步。判据是 `StepStarted` 的条数。

    按类名判而不是 isinstance：这个模块不该为了数一个数去 import 框架的事件类
    （`classify_incomplete` 同款理由）。
    """
    return sum(1 for e in events if type(e).__name__ == "StepStarted")


def hit_ceiling(used: int, tokens: int, config: AifixConfig) -> bool:
    """这一轮是**撞上我们自己的上限**才停的吗。

    分开这个判据，是为了把「翻满了没作答」与「调用本身失败」区分开 —— 两者共用
    `outcome.ok is False`，而下一步完全不同：前者调大上限或换模型，后者查端点。

    实测（2026-08-04，百炼专属网关）一次服务端 `AgentServiceGetResultError` 被
    报成「模型没能在预算内收敛」，建议是「调大 MAX_STEPS」—— 对一个服务端错误，
    调多少步都没用。

    **按我们自己的配置算，不按错误文本挑。** 错误文本是框架和端点的，随时会变；
    上一版拿子串去挑，挑不中的全落进另一档，于是 token 超限被报成「输出格式不对」。
    """
    return used >= config.reproducer_max_steps or tokens >= config.reproducer_max_tokens


def _route(config: AifixConfig):
    """复现这一步实际使用的模型路由。

    复用 `fixer` 的端点与凭据，但**思考模式单独可控且默认关**：这一步的活是
    机械的（读代码、照抄测试写法、吐 JSON），而实测有一轮的输出预算被推理全部
    吃掉、正文一个字没吐。

    `reproducer_thinking` 为 None 时不发这个参数（随端点默认）；fixer 自己那条
    路由**不受影响** —— 它要看测试反馈迭代补丁，推理对它可能真有用。
    """
    if config.reproducer_thinking is None:
        return config.fixer
    return config.fixer.model_copy(update={
        "llm_extra_body": {**config.fixer.llm_extra_body,
                           "enable_thinking": config.reproducer_thinking}})


def build_reproduce_registry(sandbox: Sandbox,
                             adapter: ProjectAdapter) -> ToolRegistry:
    """reproducer 的能力面：**只读**，四个工具。

    没有 apply_patch、也没有 edit_file：复现测试由确定性代码写下去，不经过
    工具面——这正是「不许改测试文件」那道守卫不用为 M6 改一行的原因。给了
    任何一条写入路径，它就能直接改产品代码去迎合自己写的测试。

    没有 run_tests：让它自己跑测试，「这条测试红不红」的判定权就落到了模型
    手里。红检是这一步唯一的确定性证据，不能交出去。

    adapter 目前只用于保持与 build_registry 一致的签名；红检那边才真正用到它。
    """
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ReadSymbolTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    return reg


def write_reproduction(worktree: Path, r: Reproduction) -> Path:
    """把测试写进工作区，返回落盘路径。**绝不覆盖已有文件。**

    建父目录：模型完全可能给出 `tests/regression/test_x.py`，而那个子目录
    未必存在。路径安全（相对、无 `..`、落在 test_dirs 之下）已在
    parse_reproduction 里校验过，这里不重复判——重复判两份会各自漂移。

    **撞名改名，不是拒绝**（2026-08-01 的功能巡检逼出来的）：拿一个真实小
    仓库跑 `aifix reproduce`，模型给出的路径是 `tests/test_calc.py` —— 仓库
    里已经有的那个。这不是意外：给 `calc.py` 的缺陷写测试，任何人都会挑那个
    文件。命令行那侧当时拒绝并退出，**而 issue 那条路直接写了下去**：

        整个 test_calc.py 被一条生成的测试替换
          → commit 进交付分支 → baseline 跑起来，原来那些用例已经不存在
          → 「这个补丁没弄坏别的」在一个少了一堆用例的对照组上成立
          → PR 里躺着一次删测试的改动，而报告说一切正常

    守卫放在这里而不是两个调用方各一份 —— 各写各的正是它当初只存在于命令行
    那一侧的原因。

    改名同时**改写 `target_test_id`**：不改的话写下去的是 A、跑起来的是 B，
    红检会报「这个用例没跑出结果」，而真相是它在另一个文件里。改的是 `r`
    自身（调用方随后要拿 `r.test_file` 去 `git add`、拿 id 去红检），返回值
    也是真正写下去的那个路径。
    """
    base = Path(worktree) / r.test_file
    base.parent.mkdir(parents=True, exist_ok=True)

    p = base
    n = 0
    while p.exists():
        n += 1
        # 后缀带 `aifix`：人在 PR 里看到这个文件名，要能一眼认出它是机器加的。
        # 仍以 `test_` 开头、仍在同一个测试目录下 —— 挪出去的话「不许改测试
        # 文件」那道守卫按 test_dirs 判定时就不认它了。
        suffix = "_aifix" if n == 1 else f"_aifix_{n}"
        p = base.with_name(f"{base.stem}{suffix}{base.suffix}")

    if p != base:
        new_rel = p.relative_to(Path(worktree)).as_posix()
        # id 的形状是 `<file>::<用例>`，只换前缀那一段
        _, sep, tail = (r.target_test_id or "").partition("::")
        r.test_file = new_rel
        r.target_test_id = f"{new_rel}{sep}{tail}" if sep else new_rel

    p.write_text(r.test_code or "", encoding="utf-8")
    return p


# 「这个名字/模块根本不存在」这一类异常。第 1 条闸已经挡掉了它在**收集阶段**
# 的形态（模块级 import 失败 → 文件级 id），这里挡的是同一类错误发生在**运行
# 时**：模块 import 得好好的，名字错在测试函数体里，于是前三道闸全部放行——
# 文件收集正常、用例跑了、用例也确实红了。
#
# 真实代价（2026-08-02，issue #9 的真跑）：模型写的复现用了 `pytest.raises`
# 却没有 `import pytest`，测试红在自己的 NameError 上。红检放行，fixer 对着
# 这个假靶子改了两轮、$1.45 / 468k tokens，两轮都引入回归、都被三态判决回滚。
# 最后给人的报告是「没修好」，而真正的原因是复现测试从一开始就是错的——
# 报告里没有任何一句话指向这一点。
#
# 只收「名字/模块不存在」，**刻意不收 AttributeError**：`app.foo()` 没有这个
# 属性既可能是模型猜错了 API，也可能正是 issue 要报的缺陷本身，分不开的信号
# 不该拿来拦人。
_MISSING_NAME = frozenset({"NameError", "ModuleNotFoundError", "ImportError"})


def _typo_reason(failure: Failure | None, worktree: Path,
                 adapter: ProjectAdapter) -> str:
    """这条测试是不是红在模型自己写的那一行上。是就给理由，不是就返回空串。

    判据是**异常抛在哪，不是异常是什么类型**。产品代码里真的引用了未定义的
    名字，那是货真价实的缺陷，NameError 正是它该有的样子——按类型一刀切会把
    这种真缺陷一并打回。区别在栈帧：笔误的栈只到复现测试文件里（那段代码整个
    是模型写的，产品代码没有办法让它抛 NameError），真缺陷的栈会**穿进产品
    文件**。实测三种形态（2026-08-03）：

        笔误 `pytest.raises` 没 import  → [(测试文件, 6)]
        产品代码里的 NameError          → [(calc.py, 5), (测试文件, 12)]
        真断言失败                      → [(测试文件, 16)]

    第三行是这道闸最要紧的边界：**真断言失败的栈同样只到测试文件**（被调函数
    正常返回了，栈上没有它），而那是最常见的合法复现。所以类型判定不能省——
    单看栈帧会把每一条正常的复现测试都打回去。

    **拿不到栈帧时一律放行。** 解析不出帧的原因有好几种（报告形状变了、路径
    不在 repo 内、适配器换了），把「没有证据」当成「有罪的证据」会让这道闸在
    自己瞎掉的时候变得最严厉，而那正是最不该拦人的时候。
    """
    if failure is None:
        return ""
    # message 形如 `NameError: name 'pytest' is not defined`。用它而不是 trace：
    # 它是异常的 repr，天生不带色码，也不含被回显的源码。
    exc = failure.message.split(":", 1)[0].strip()
    if exc not in _MISSING_NAME:
        return ""
    frames = [c for c in adapter.locate_source(failure, worktree)
              if c.origin == "traceback"]
    # origin 必须筛：import 那一档是「测试文件 import 了它」，不是「失败穿过
    # 了它」——拿它当「栈穿进了产品代码」会让这道闸永远不触发。
    if not frames:
        return ""
    test_file = failure.file or ""
    if any(c.path != test_file for c in frames):
        return ""                       # 栈穿出了测试文件 → 当作真缺陷
    return (
        f"复现测试红在**它自己**的 {exc} 上（`{failure.message.strip()}`）。\n"
        f"  栈帧只到 `{test_file}`，没有穿进任何产品代码——这条测试是因为它"
        "自己写错了名字才红的，不是因为缺陷。放过它，fixer 会对着一个假靶子"
        "改代码。\n"
        "  最常见的成因：用了 `pytest.raises` 却没有 `import pytest`。")


async def red_check(worktree: Path, adapter: ProjectAdapter,
                    target_id: str, timeout: float = 600.0) -> tuple[bool, str]:
    """复现测试必须**红**，而且要红得有信息量。零 LLM。

    四种「不算复现」分开报，因为它们指向完全不同的下一步：

    1. **收集错误**——import 不到东西也是红，但它复现的是模型自己的笔误。
       新功能的测试红在 ImportError 上是常态（函数还不存在）；修 bug 不是，
       产品代码就在那儿。放过这一类，fixer 会被派去修一个不存在的模块。
    2. **用例没跑出结果**——node id 不存在、被跳过、收集没走到它。
    3. **跑了但没失败**——这条测试在当前代码上就是绿的，约束力为零。
    4. **红在自己的笔误上**——第 1 条的运行时版本，见 `_typo_reason`。

    判定顺序不能换：收集失败时 target 必然也「没跑出结果」，先判后者会给出
    一句指错方向的话（本项目最贵的失败一向不是崩溃，是指错方向的诊断）。
    第 4 条只能排在最后——它要读那次失败的栈帧，而前三条成立时压根没有失败
    对象可读。
    """
    fs = await run_scoped(worktree, [adapter], [target_id],
                          timeout=timeout)

    stuck = file_level_ids(sorted(fs.ids), [adapter])
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

    typo = _typo_reason(fs.failures.get(target_id), worktree, adapter)
    if typo:
        return False, typo

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
            client=client or OpenAICompatibleClient(_route(config)),
            registry=build_reproduce_registry(sandbox, adapter),
            context=ContextManager(SYSTEM_PROMPT),
            # 刻意小于 fixer_max_steps：reproducer 只有读工具，读够了就该
            # 作答，多给的步数不会变成更好的测试，只会变成更长的翻阅。
            max_steps=config.reproducer_max_steps,
            # 独立的 token 上限，不吃整个 run 的额度 —— 见
            # config.reproducer_max_tokens 上面那段实测。
            budget=BudgetTracker(max_tokens=config.reproducer_max_tokens,
                                 max_wall_seconds=config.budget_wall_seconds),
            loop_detect_window=config.loop_detect_window,
            tool_result_max_chars=config.tool_result_max_chars,
            model_name=config.fixer.model,
            price_map=config.price_map,
        )
        prompt = build_prompt(issue_title, issue_body, adapter.test_dirs(),
                              max_steps=config.reproducer_max_steps,
                              example_id=adapter.example_test_id())
        # 美元闸：复现最多用掉整份预算的 reproducer_budget_share。
        # 不设的话它能把修复那一步饿死（见 config 里那段实测）。
        # budget_usd 为 0 时传 None —— 那是「不设闸」，与「额度已扣光」不同，
        # 而 `0.0 * 0.4 or None` 求值成 None 恰好把两者混掉（fix_node 里同款坑）。
        cap = (config.budget_usd * config.reproducer_budget_share
               if config.budget_usd else None)
        outcome = await consume(loop.run(prompt), cost_cap=cap)
    finally:
        await sandbox.close()

    common = dict(tokens=outcome.tokens, cost_usd=outcome.cost_usd,
                  events=outcome.events, event_times=outcome.event_times)
    if not outcome.ok:
        # `outcome.ok is False` 意味着**循环自己没跑完**（步数耗尽、token 超限、
        # 崩了），它压根没产出最终文本 —— 所以这一支**不可能**是解析问题。
        #
        # 这里曾经拿 `"max_steps" in err` 去挑，挑不中的落进 UNPARSEABLE：于是
        # 第三次真跑（2026-07-30）token 超限被报成「模型的输出解析不出复现测试」
        # ——一句指向模型输出格式的话，而真相是额度不够。与上一版把「没收敛」
        # 报成「issue 信息不足」是同一个错，只是换了一条兄弟分支。
        #
        # 按**结构**分而不是按字符串挑：错误文本是框架的，随时会变；
        # 「循环有没有跑完」是我们自己的判据。
        err = outcome.error or ""
        # **「翻满了没作答」与「调用本身失败」不是一回事**，而它们共用
        # `outcome.ok is False`。实测（2026-08-04，百炼专属网关）一次
        # `AgentServiceGetResultError` 被报成「模型没能在预算内收敛」，给出的
        # 下一步是「调大 MAX_STEPS / MAX_TOKENS」—— 对一个服务端错误，调多少
        # 步都没用。指错方向的诊断，这个项目最忌讳的那种。
        #
        # 判据按**我们自己的上限**算，不按错误文本挑（错误文本是框架和端点的，
        # 随时会变 —— 上一版拿 `"max_steps" in err` 挑就是这么栽的）：真撞上限
        # 的话，步数或 token 必然已经贴到配置值；没贴到就说明是半路断的。
        used = steps_used(outcome.events)
        if not hit_ceiling(used, outcome.tokens, config):
            return ReproduceOutcome(
                None,
                f"模型调用本身失败了：{err}\n"
                f"  它只走到第 {used} 步（上限 {config.reproducer_max_steps}）、"
                f"用了 {outcome.tokens:,} token（上限 "
                f"{config.reproducer_max_tokens:,}）—— **没有撞上任何一道闸**，"
                "所以调大上限不解决问题。\n"
                "  这既不是 issue 写得不清楚，也不是模型答不好。\n"
                "  下一步：查端点与凭据（`AIFIX_FIXER__BASE_URL` / "
                "`AIFIX_FIXER__API_KEY`），确认那个端点支持带工具调用的多步对话；"
                "或直接重试一次 —— 服务端错误常常是一过性的。",
                kind=KIND_CALL_FAILED, **common)
        return ReproduceOutcome(
            None,
            f"模型没能在预算内给出复现测试（{err}）。\n"
            f"  当前上限：{config.reproducer_max_steps} 步 / "
            f"{config.reproducer_max_tokens:,} token。\n"
            "  这**不是 issue 写得不清楚** —— 补充 issue 不解决它。\n"
            "  下一步：调大 `AIFIX_REPRODUCER_MAX_STEPS` / "
            "`AIFIX_REPRODUCER_MAX_TOKENS`，或换一个更会收敛的模型；"
            "events.jsonl 里有它这几步在读什么。",
            kind=KIND_NO_CONVERGENCE, **common)

    if outcome.cost_capped:
        # **必须排在截断判定之前**：成本闸触发时 consume 主动关掉生成器，事件流
        # 里同样没有 RunFinished —— 两者的签名一模一样，而原因相反。实测
        # （2026-07-30，issue #2）两轮的累计成本是 $0.2179 / $0.2070，闸是
        # $0.50 × 0.4 = $0.20，被上一版报成了「查端点是不是在掐流」。
        cap = config.budget_usd * config.reproducer_budget_share
        return ReproduceOutcome(
            None,
            f"复现这一步撞上了**它自己的美元闸**（上限 ${cap:.4f} = "
            f"AIFIX_BUDGET_USD × {config.reproducer_budget_share}）。\n"
            "  不是模型的问题，也不是端点的问题 —— 是这次给它的钱不够走完。\n"
            "  下一步：调大 `AIFIX_BUDGET_USD`、或调大 "
            "`AIFIX_REPRODUCER_BUDGET_SHARE`、或把这一步换成便宜的模型"
            "（`AIFIX_FIXER__MODEL`）。",
            kind=KIND_COST_CAPPED, **common)

    if classify_incomplete(outcome.events or []):
        # 先判这一条：流断了的话，正文必然是残的，后面两条判据看到的都是
        # 半截东西，给出的诊断会指向模型而不是这次调用。
        return ReproduceOutcome(
            None,
            "这次模型调用**中途断了**（有一步只开始、没结束），拿到的正文是残的。\n"
            "  不是模型答歪了，也不是 issue 的问题 —— 下一步是重试，"
            "或查端点是不是在长响应上掐流。\n"
            "  events.jsonl 里能看到它断在哪一步。",
            kind=KIND_TRUNCATED, **common)

    if not (outcome.text or "").strip():
        # 一个字都没吐。推理型模型（deepseek 的 reasoning_content、o 系列的
        # reasoning tokens）会把输出预算整个烧在推理里然后被截断 —— 实测
        # （2026-07-30，issue #2）事件流里 ReasoningDelta 1001 条、TextDelta 0
        # 条，第 9 步连 StepFinished 都没有，最后一条 ModelUsage 的 token 数是
        # None。
        #
        # 归进 unparseable 是错的：那句话让人去看它吐了什么，而它什么都没吐。
        reasoning = sum(1 for e in (outcome.events or [])
                        if type(e).__name__ == "ReasoningDelta")
        hint = (f"事件流里有 {reasoning} 条推理增量、0 条正文 —— "
                "它把输出预算全烧在**推理**里，正文被截断了。\n"
                "  下一步：换一个推理更短的模型，或在端点侧压低 reasoning 长度。"
                if reasoning else
                "事件流里既没有正文也没有推理 —— 多半是上游把响应截断了。")
        return ReproduceOutcome(
            None, f"模型没有吐出任何正文。\n  {hint}",
            kind=KIND_EMPTY_ANSWER, **common)

    r, why = parse_reproduction_ex(outcome.text, adapter.is_test_path)
    if r is None:
        return ReproduceOutcome(
            None,
            f"{why}。\n"
            "  同样不是 issue 的问题；events.jsonl 里有它实际吐出来的东西。",
            kind=KIND_UNPARSEABLE, **common)
    if not r.can_reproduce:
        return ReproduceOutcome(
            r, "issue 里的信息不足以写出复现测试，还缺：\n"
            + "\n".join(f"  - {m}" for m in r.missing_info),
            kind=KIND_MISSING_INFO, **common)
    return ReproduceOutcome(r, **common)
