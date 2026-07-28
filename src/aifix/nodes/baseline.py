from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.sandbox.local import LocalSandbox

from ..adapters.base import ProjectAdapter
from ..adapters.junit import parse_junit
from ..adapters.maven_adapter import MavenAdapter
from ..adapters.pytest_adapter import PytestAdapter
from ..graph import AifixState

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
def adapter_for(name: str) -> ProjectAdapter:
    return ADAPTERS[name]()


def _check_report(worktree: Path, paths: list[Path], required: bool) -> None:
    """required 时至少要有一份报告，否则抛 —— 「没跑成」不能冒充「跑完了、全绿」。

    parse_junit 对缺失报告的处理是安全跳过并返回空集合。对核心循环这是对的
    （少一份报告不该让整个 run 崩掉，下一轮 verify 会重新跑）；但对挖任务是
    致命的：空集合会被读成「全绿」，`red - green` 于是把 base 处所有红的用例
    全部当成「红转绿」吐出来。报告缺失的真实含义是超时被杀 / 进程崩溃 /
    沙箱执行失败，这时唯一安全的动作是让调用方知道并跳过。

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

    require_report 默认 False：核心循环容忍报告缺失（见 _check_report）。
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
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。"""
    adapter = adapter_for(state["adapter_name"])
    fs = await run_full_suite(Path(state["worktree_path"]), adapter)
    ids = sorted(fs.ids)
    return {"baseline_ids": ids, "queue": list(ids),
            "_failures": dict(fs.failures)}
