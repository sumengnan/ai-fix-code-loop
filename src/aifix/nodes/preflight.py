from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from ..config import AifixConfig
from ..delivery import ensure_clean
from ..graph import PREFLIGHT_ABORT_KIND, AifixState
from .baseline import ADAPTERS, detect_adapters


def _bad_test_python(configured: str | None) -> str | None:
    """显式配置的测试解释器不可用时返回中止理由，可用返回 None。

    只校验**显式配置**，不校验探测结果：探测本身已经要求文件存在且可执行，
    而这里要拦的是用户自己写错的那个值（路径打错、venv 删了、写成了目录）。

    必须在这里拦住而不是留给 baseline：到了那一步，exec 失败的表现是「没写出
    JUnit 报告」，用户看到的中止消息是「测试进程没能正常跑完」—— 一句指向
    目标项目的话，而真相是 aifix 的配置写错了。这个项目里最贵的失败一向不是
    崩溃，是指错方向的诊断。
    """
    if not configured:
        return None
    p = Path(os.path.expanduser(configured))
    if p.is_file() and os.access(p, os.X_OK):
        return None
    return (f"配置的测试解释器不可用：{configured}\n"
            "  AIFIX_TEST_PYTHON 要指向一个可执行的 Python 解释器"
            "（例如目标项目的 .venv/bin/python）。\n"
            "  不配这一项时 aifix 会自动探测源仓库里的 .venv / venv，"
            "都没有则退回 aifix 自己的解释器。")


def preflight_node(state: AifixState) -> dict[str, Any]:
    """校验测试解释器 + 探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。

    探测本身在 baseline.detect_adapter 里，只有那一份：此处曾另存一份只含
    PytestAdapter 的列表，于是 adapter_name 的真正来源和登记新适配器的地方
    不是同一处；`aifix mine` 后来也各写了一份（写死 PytestAdapter()）。

    三条拒绝路径都要带上 `abort_kind`。只写 `abort` 的后果是**静默的**
    （2026-08-01 的功能巡检撞出来）：`_cmd_run` 的退出码按 kind 判，不写就
    走不进那个集合 —— `aifix run /打错的/路径` 印一句「中止」然后**退 0**，
    流水线里 `aifix run || 报警` 一声不吭，CI 把这次读成成功，而它一个用例
    都没跑过。
    """
    bad = _bad_test_python(state["config"].test_python)
    if bad:
        return {"abort": bad, "abort_kind": PREFLIGHT_ABORT_KIND}
    repo = Path(state["repo"])
    configured = state["config"].adapters
    if configured:
        # 显式声明优先。名字写错在这里就拒，不等到 baseline 才炸 —— 那时
        # 用户看到的会是一句指向目标项目的话。
        unknown = [n for n in configured if n not in ADAPTERS]
        if unknown:
            return {"abort": f"AIFIX_ADAPTERS 里有不认识的适配器："
                             f"{'、'.join(unknown)}（可选：{'、'.join(ADAPTERS)}）",
                    "abort_kind": PREFLIGHT_ABORT_KIND}
        names = list(configured)
        # 配了但探测不到的那些，**出声不拦**。
        #
        # 出声的理由：不说的话，baseline 会去跑一个根本不存在的命令，报告写的是
        # 「测试进程没能正常跑完」—— 一句指向目标项目的话，而真相是这个变量配
        # 错了。`AIFIX_ADAPTERS` 是新旋钮，配错的概率不低。
        #
        # 不拦的理由：`detect` 是启发式的（vitest 那条要求依赖里**直接**列了
        # vitest，而 monorepo 里靠 workspace 提上去、或者装在父目录的情形是真
        # 存在的）。拿一个可能误报的信号去拦住整个 run，会让人为了跑起来而干脆
        # 不用这个开关 —— 与 `warn_if_patch_may_be_invisible` 同一条取舍。
        missing = [n for n in names if not ADAPTERS[n].detect(repo)]
        if missing:
            print(f"⚠️  警告：AIFIX_ADAPTERS 里的 {'、'.join(missing)} "
                  f"在 {repo} 上探测不到。\n"
                  "    仍会照配置去跑；跑不起来的话报错会长得像"
                  "「测试进程没能正常跑完」，而真正的原因在这一行。",
                  file=sys.stderr, flush=True)
    else:
        # 没显式声明就**只用第一个** —— 与加多适配器之前逐字节相同的行为。
        # 理由见 config.adapters 上面那段：自动全跑会让「Java 工程带 Python
        # 胶水脚本」那类仓库在升级之后直接打不开。
        found = detect_adapters(repo)
        if not found:
            return {"abort": f"没有适配器认领这个项目：{repo}",
                    "abort_kind": PREFLIGHT_ABORT_KIND}
        names = [found[0].name]
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e), "abort_kind": PREFLIGHT_ABORT_KIND}
    return {"adapter_names": names, "abort": None}


async def probe_model(config: AifixConfig, client: Any = None) -> str | None:
    """发一次极小的调用确认模型端点可达。可达返回 None，否则返回中止理由。

    不做这一步的话，端点不通的表现是：每一轮都修不好 → 重试到上限 → 连续失败
    熔断 → 报告写「连续 N 个 failure 均未修复，疑似系统性问题」。**跑了几十
    分钟，最后给出一句指错方向的诊断**，而真相是 key 配错了或端点不通。

    与本模块开头 `_bad_test_python` 那段是同一条理由，只是换了一个依赖：能在
    一秒内确定的前提，不该拖到几分钟后以别人的面目暴露。

    探的是 **fixer** 那条路由：detector 可以配成另一个端点，但真正干活、也真正
    花钱的是 fixer；两条都探等于把最常见的场景（同一个端点）探两遍。detector
    自己不通时仍会以「诊断解析失败」降级，那条路径本来就有兜底。

    只读第一个 chunk 就够：要确认的是**连得上、认得出凭据**，不是模型会说什么。
    """
    from harness.llm.openai_compat import OpenAICompatibleClient
    from harness.types import Message, Role

    try:
        # 构造也必须在 try 里：没配 API key 时客户端在**构造阶段**就抛，而那
        # 是最常见的第一次失败。留在外面的话异常会裸穿出 run_once（探针挡在
        # 它那个 try 之前），用户拿到一段 openai 的调用栈、没有报告、没有下
        # 一步 —— 而这恰恰是 preflight 存在的全部理由。
        c = client or OpenAICompatibleClient(config.fixer)
        async with contextlib.aclosing(
                c.stream([Message(role=Role.USER, content="ping")], [])) as st:
            async for _ in st:
                break
    except Exception as e:
        return (f"模型端点不可达：{type(e).__name__}：{e}\n"
                f"  这不是修复失败，是这次 run 还没开始就没跑起来。\n"
                f"  本次用的模型：{config.fixer.model}\n"
                f"  依次检查 AIFIX_FIXER__BASE_URL / AIFIX_FIXER__API_KEY，"
                f"以及这台机器能不能出网到那个地址。\n"
                f"  在 GitHub Actions 上尤其要确认端点**没有 IP 白名单** —— "
                f"runner 的出口 IP 是动态的。")
    return None
