from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from harness.sandbox.local import LocalSandbox

from ..adapters.base import ProjectAdapter
from ..adapters.junit import parse_junit
from ..adapters.maven_adapter import MavenAdapter
from ..adapters.pytest_adapter import (PytestAdapter, imports_outside_worktree,
                                       resolve_test_python)
from ..graph import AifixState, trace_of

# 全项目唯一的适配器注册表。preflight_node 按插入顺序逐个 detect()，
# adapter_for 按名字取，两种用法共用这一份数据 —— 曾经
# preflight 另存一份 `ADAPTERS = [PytestAdapter]`，而 adapter_name 由**它**
# 决定：MavenAdapter 在这边登记好了，Maven 工程走到 preflight 照样 abort，
# 加了等于没加，且两处都不会报错。
#
# **dict 的插入顺序就是探测顺序，改动顺序等于改变探测语义。**
# Maven 在前：MavenAdapter.detect 要求根目录有 pom.xml，是一个具体且几乎
# 不会误判的信号；PytestAdapter.detect 极宽松 —— pyproject.toml 或 tests/
# 存在就认领，而 Java 工程的工具链里带 Python 脚本（发版、代码生成、CI 胶水）
# 是常事。反过来排的后果不是报错而是静默：Maven 工程被判成 pytest 工程，
# baseline 跑 pytest 命令收不到任何用例，报告写「0 个失败」。
# 通则：detect 越具体的排越前，兜底式的排最后。
ADAPTERS: dict[str, type[ProjectAdapter]] = {
    "maven": MavenAdapter, "pytest": PytestAdapter}


# 返回类型是协议而不是某个具体适配器：注册表里现在有两个实现，写死其中
# 一个会让另一个在类型上「碰巧也能用」。
def adapter_for(name: str, python: str | None = None) -> ProjectAdapter:
    """按名字取适配器。python 是跑测试用的解释器，None 表示各实现自己的默认。

    注入走构造参数而不是改协议：`full_test_command()` 不接参数是协议里写死
    的（报告写到哪、用什么命令都是构建体系自己的事），而 MavenAdapter 根本
    不需要解释器。加一个方法参数要让两个实现和五个调用点一起跟上，加一个
    构造参数只要注册表这一行。
    """
    return ADAPTERS[name](python=python)


def adapter_from_state(state: AifixState) -> ProjectAdapter:
    """核心循环取适配器的**唯一**入口 —— 解释器在这里注入。

    为什么不是各节点自己 `adapter_for(name)`：解释器要同时看配置和**源仓库**
    （`state["repo"]`），而不是 worktree —— worktree 与评测的克隆里都没有
    `.venv`（它没被 git 跟踪）。只有 state 同时握着这两样。

    四个节点都必须走这里，漏掉一个的代价不对称：detect 只用 locate_source，
    漏了看不出来；但 fix 漏掉的话，FixerAgent 手里的 RunTestsTool 会用另一个
    解释器复跑 —— 模型看到的证据和 verify 的判定依据不是同一套环境，而两边
    都不会报错。tests/test_interpreter.py 有一条对着源码的断言钉这件事。
    """
    return adapter_for(
        state["adapter_name"],
        python=resolve_test_python(Path(state["repo"]),
                                   state["config"].test_python))


def warn_if_patch_may_be_invisible(state: AifixState,
                                   adapter: ProjectAdapter) -> None:
    """跑 baseline 之前问一句：目标包会不会从 worktree **之外**导入。

    换用目标项目自己的解释器换来的真实风险 —— 目标项目若把自己可编辑安装进
    了那个 venv，`import <目标包>` 可能解析到源仓库那份**没打补丁**的代码，
    于是每一轮 verify 验的都是原代码：不崩溃、不报错，只有「修好了」是假的。

    只出声、不中止：这道探测是近似的（见 imports_outside_worktree），拿一个
    可能误报的信号去拦住整个 run，会让用户为了跑起来而去关掉它，那比没有更糟。

    写 stderr 而不是等报告：报告在整个 run 结束后才渲染，而这句话要在那之前
    说出来才有用 —— 它要挡住的正是「跑了半小时、花了钱、结论是假的」。
    trace 里另记一份事实，事后能查。
    """
    if not isinstance(adapter, PytestAdapter):
        return                                  # Maven 不走 Python 的 import
    trace_of(state).fact("test_python", adapter.python)
    hits = imports_outside_worktree(adapter.python,
                                    Path(state["worktree_path"]))
    if not hits:
        return
    trace_of(state).fact("imports_outside_worktree",
                         [{"module": m, "origin": o} for m, o in hits])
    lines = "\n".join(f"    {m} → {o}" for m, o in hits)
    print(
        "⚠️  警告：下列顶层包在测试解释器里解析到了 worktree 之外，"
        "本次验证很可能跑的是**没打补丁**的代码：\n"
        f"{lines}\n"
        f"    worktree：{state['worktree_path']}\n"
        f"    测试解释器：{adapter.python}\n"
        "    常见成因：目标项目以可编辑方式装进了这个解释器"
        "（pip install -e .），而 pytest 没有把 worktree 的源码目录插到 "
        "sys.path 更前面。\n"
        "    修法：在目标项目的 pytest 配置里加 pythonpath，例如 "
        'pyproject.toml 的 [tool.pytest.ini_options] 下 pythonpath = ["src"]。',
        file=sys.stderr, flush=True)


def detect_adapter(repo: Path,
                   python: str | None = None) -> ProjectAdapter | None:
    """按注册表顺序探测这个仓库归谁管；没人认领返回 None。

    **全项目唯一的探测入口**，preflight_node 与 `aifix mine` 都走这里。
    分头写就是「第二份注册表」那处裂缝的形状：`_cmd_mine` 曾直接
    `PytestAdapter()`，于是对着 Maven 仓库 source_suffixes() 只认 `.py` →
    gold_files 恒空 → is_candidate 恒 False → 产出 0 个任务，不报一个错，
    与「这个仓库最近没有红转绿的提交」完全无法区分 —— 而适配层里为 Maven
    补的每一处缺口都在这一行之后，全都到不了。
    """
    for cls in ADAPTERS.values():
        if cls.detect(Path(repo)):
            return cls(python=python)
    return None


def _check_report(worktree: Path, paths: list[Path], required: bool) -> None:
    """required 时至少要有一份报告，否则抛 —— 「没跑成」不能冒充「跑完了、全绿」。

    parse_junit 对缺失报告的处理是安全跳过并返回空集合。这个默认曾经覆盖到
    核心循环，理由写的是「少一份报告不该让整个 run 崩掉，下一轮 verify 会重新
    跑」——**那句话站不住**：baseline 一次 run 只跑一次，没有下一轮；而 verify
    这一轮的空集合当场就会被读成「全绿」并 commit。核心循环的三个调用点因此
    都改成了 required（见 baseline_node 与 verify_node），默认值只留给别的
    调用方。

    挖任务那条路径上它一直是致命的：空集合会被读成「全绿」，`red - green`
    于是把 base 处所有红的用例全部当成「红转绿」吐出来。报告缺失的真实含义是
    超时被杀 / 进程崩溃 / 沙箱执行失败，这时唯一安全的动作是让调用方知道并跳过。

    消息里不点名具体文件：报告可以有多份（surefire 每个测试类一份），
    点名某一个在那种适配器上就是一句假话。
    """
    if required and not paths:
        raise RuntimeError(
            f"测试未产出任何 JUnit 报告（worktree={worktree}）："
            "测试进程没能正常跑完（超时被杀 / 崩溃 / 沙箱执行失败），"
            "本次结果不可信")


async def _rm_reports(sb: LocalSandbox, adapter: ProjectAdapter,
                      worktree: Path, scoped: bool) -> None:
    """删掉本次跑出的报告 —— 可能不止一份。

    理由是**陈旧报告会被下一跑当成自己的结果**：report_paths 只看文件系统
    当前状态，留在原地的上一轮报告会被 parse_junit 一并解析（见
    maven_adapter.report_paths 的说明）。flaky 确认据此判定，不报错，只是判错。

    此处原先写的是「Worktree.commit() 的 git add -A 会把它扫进交付分支」——
    那是假话：commit 只 `git add -- <ApplyPatchTool 记账过的路径>`，它的
    docstring 里专门写着绝不用 git add -A。tests/test_maven_e2e.py 里有一条
    真跑 mvn 的验收：交付分支的树上连整个 target/ 都没有。
    """
    stale = adapter.report_paths(worktree, scoped=scoped)
    if stale:
        await sb.exec(["rm", "-f", *(str(p) for p in stale)], 10.0)


async def run_full_suite(worktree: Path, adapter: ProjectAdapter,
                         timeout: float = 900.0,
                         require_report: bool = False):
    """在 worktree 里跑全量测试并解析报告。零 LLM。

    require_report 默认 False，但核心循环的每一个调用点都显式传了 True
    （见 _check_report）：默认值留给「少一份报告确实无所谓」的调用方，
    产品路径上不存在这样的调用方。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.full_test_command(), timeout)
        paths = adapter.report_paths(worktree)
        _check_report(worktree, paths, require_report)
        return parse_junit(paths, adapter.make_test_id)
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=False)
        await sb.close()


async def run_scoped(worktree: Path, adapter: ProjectAdapter,
                     test_ids: list[str], timeout: float = 300.0,
                     require_report: bool = False):
    """只跑指定用例并解析报告。供 flaky 确认使用 —— 成本远低于全量。

    走 scoped 那份报告：调用它的时候全量那份通常还要继续用，不能被覆盖。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        await sb.exec(adapter.scoped_test_command(test_ids), timeout)
        paths = adapter.report_paths(worktree, scoped=True)
        _check_report(worktree, paths, require_report)
        return parse_junit(paths, adapter.make_test_id)
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=True)
        await sb.close()


async def baseline_node(state: AifixState) -> dict[str, Any]:
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。

    `require_report=True`：baseline 是整个 run 唯一一次「改动之前长什么样」
    的测量，没有下一轮可以兜底。测试进程没跑成时返回空集合，队列就是空的，
    run 以「修复 0 / 0、全绿、没活干」正常收场、退出码 0 —— 用户得到的是一句
    「你的仓库没问题」，而真相是测试压根没跑起来。
    """
    adapter = adapter_from_state(state)
    warn_if_patch_may_be_invisible(state, adapter)
    fs = await run_full_suite(Path(state["worktree_path"]), adapter,
                              require_report=True)
    ids = sorted(fs.ids)
    return {"baseline_ids": ids, "queue": list(ids),
            "_failures": dict(fs.failures)}
