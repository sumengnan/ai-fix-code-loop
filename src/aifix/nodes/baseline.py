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
from ..graph import COLLECTION_ABORT_KIND, AifixState, trace_of
from ..testenv import sanitized_command

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


# 判据的两个阈值，**同时成立**才中止。分开两条是因为它们防的不是同一件事：
#
# 条数下限防的是「小样本上的比例没有意义」。单独一条文件级 error 极可能就是
# 这个仓库自己的 bug（某个模块被改名、忘了提交一个文件），那正是 aifix 该修
# 的活；而且代价有界 —— 队列里就它一条，最多烧掉一个 failure 的预算。用比例
# 拦它的话（1/1 = 100%）aifix 会在一整类正常仓库上打不开。
#
# 比例下限防的是「个别文件导不进来」被误当成环境故障。严格过半而不是「一条
# 都不许」：真实仓库里确实可能有个别测试文件本来就 import 失败。
#
# 两条阈值在 pytest 与 Maven 上的做功并不一样，这一点必须写明白：**pytest
# 只要有一条收集错误就会中断整轮收集**（实测 pytest 9.1.1，exit 2，报告里
# 只剩那几条文件级 <error>，别的测试一个都没跑），所以 pytest 侧的占比在有
# 收集错误时**恒为 1.0**，真正做功的只有条数那一条。比例那一条是给 Maven
# 用的：surefire 不中断，一个测试类 @BeforeAll 炸了别的类照跑，报告里
# 类级 error 与用例级失败是真会混在一起的。
COLLECTION_ABORT_MIN_COUNT = 2
COLLECTION_ABORT_RATIO = 0.5


def file_level_ids(ids: list[str], adapter: ProjectAdapter) -> list[str]:
    """baseline 里哪些 id 指的是一整个测试文件 / 测试类，而不是单个用例。

    判据问适配器，不自己写一套语法。`eval/mine` 曾写死 `"::" not in i`，
    而 `::` 是 pytest 的语法 —— Maven 的 id 一个都没有，于是**每一个**
    Maven id 都被判成文件级。这里若重蹈覆辙，后果是对称的：一个纯用例
    失败的 Maven baseline 会被整个误判成环境故障、当场中止。
    """
    return [i for i in ids if adapter.is_file_level_id(i)]


def _collection_hint(adapter: ProjectAdapter) -> list[str]:
    """针对具体适配器的「下一步该干什么」。

    只有 pytest 那条路才谈解释器：Maven 走的是 `mvn`，劝人换 Python 解释器
    是一句假话，而本项目把「消息说了一件代码没做的事」与数字造假同等对待。
    """
    if not isinstance(adapter, PytestAdapter):
        return ["  最常见的成因是构建环境不完整（依赖没拉全、离线仓库缺件）—— "
                "先在目标项目里手工跑一次测试命令，确认它自己能跑起来。"]
    return [
        "  最常见的成因：跑测试用的解释器里没有目标项目的测试依赖"
        "（典型报错 ModuleNotFoundError: No module named '...'）。",
        f"  本次用的测试解释器：{adapter.python}",
        "  修法：把 AIFIX_TEST_PYTHON 指向**目标项目自己**的解释器，例如",
        "    export AIFIX_TEST_PYTHON=/path/to/目标项目/.venv/bin/python",
        "  先自己确认一句：那个解释器在目标项目里 `-m pytest -q` 能正常收集。",
    ]


def collection_error_abort(ids: list[str],
                           adapter: ProjectAdapter) -> str | None:
    """baseline 里文件级 error 占比过高 → 返回中止理由；否则 None。

    存在的理由（实测挖出来的）：pytest 收集阶段整轮中断时**照样写出一份完整
    的 JUnit 报告**，里面是一条条文件级 `<error>`。`_check_report` 拦的是
    「一份报告都没写出来」，所以它一个异常都不会抛 —— 那些 error 被
    `make_test_id` 老老实实翻译成可重跑的 node id，进 baseline_ids，进 queue。
    于是 Detector 和 Fixer 被派去修「目标机器上没装某个包」这件事：真花钱，
    连续失败熔断在第 3 个之后中止，而报告里那 3 行写的是「模型没修好」——
    一个成绩，其实是一次故障；走评测的话会被记成模型的失分。

    **中止而不是过滤**。过滤（把文件级 id 从队列里剔掉、剩下的真失败照修）
    在这里是错的，有两个理由。一是 pytest 侧过滤完队列必然是空的（收集一中
    断，报告里除了这些 error 什么都没有），「还剩的真失败」并不存在，run 会
    以「修复 0 / 0、全绿、没活干」收场 —— 正是这条修复要消灭的那副样子。
    二是更根本的：收集错误意味着**这份 baseline 是残缺的**，pytest 压根没跑
    到后面的测试，我们不知道这个仓库其余部分是红是绿。悄悄扔掉几条 id 会把
    「测量没做成」伪装成「测量做完了，只是少几条」。测量不可信时唯一诚实的
    动作是停下来说清楚。

    返回消息而不是抛异常：调用方要把它变成一次**有种类的中止**（报告照渲染、
    评测记成故障而不是失分），抛出去会被 run_once 记成 crash。
    """
    if not ids:
        return None
    bad = file_level_ids(ids, adapter)
    if len(bad) < COLLECTION_ABORT_MIN_COUNT:
        return None
    if len(bad) <= len(ids) * COLLECTION_ABORT_RATIO:
        return None
    shown = bad[:5]
    more = f"（共 {len(bad)} 条，只列前 {len(shown)} 条）" if len(bad) > len(shown) else ""
    return "\n".join([
        # 这句话既做中止理由、又做开了逃生口时的警告，所以**不写「已中止」**：
        # 逃生口开着的时候它并没有中止，一句自相矛盾的消息比没有消息更坏。
        # 「中止」二字由报告的 `> **中止**：` 前缀负责说。
        f"本次 baseline 的 {len(ids)} 个失败里有 {len(bad)} 个是"
        "**整个测试文件 / 测试类没能跑起来**（收集阶段就失败，"
        "不是某个用例断言不过）—— 这批 baseline 不可信。",
        "  **这不是模型的问题，也不是这些测试本身的问题**："
        "把它们当成待修用例排进队列，Detector 与 Fixer 会被派去修"
        "「这台机器上缺了点什么」这件事，会真花钱，而报告最后写的是"
        "「模型没修好」—— 一个成绩，其实是一次故障。",
        *_collection_hint(adapter),
        f"  没跑起来的{more}：",
        *(f"    {i}" for i in shown),
        "  如果确认这些**就是**这个仓库自己的 bug（例如某个模块被改名，"
        "几个测试文件一起 import 不到），设 AIFIX_ALLOW_COLLECTION_ERRORS=1 "
        "跳过这道闸，照常排队开修。",
    ])


def _check_report(worktree: Path, paths: list[Path],
                  ran: frozenset[str], required: bool,
                  res: Any = None, timeout: float | None = None) -> None:
    """required 时报告要存在**且真的跑了用例**，否则抛。

    「没跑成」不能冒充「跑完了、全绿」，而它有两种形状，光查文件在不在只挡得住
    第一种：

    - **一份报告都没有** —— 进程没跑完：超时被杀、崩溃、命令根本没执行起来。
    - **报告在，里面零个用例** —— 进程正常退出了，是收集没成功：node id 无效、
      conftest 抛异常、测试依赖缺失。pytest 此时退 4 并写出一份 `tests="0"` 的
      报告（实测，pytest 9.1.1）。文件存在，旧的检查放行，parse_junit 解出空
      集合，于是 baseline 读成「全绿」、verify 读成「补丁修好了一切」——
      这道闸要挡的事换个形状就绕过去了。

    两种形状的排查方向完全不同，消息也分开：给错方向的提示比不给更费时间。

    「零个用例」与「这个仓库真的没有测试」确实分不开，此处**有意把后者也拦下**：
    适配器已经认领了这个仓库（`tests/` 或 pyproject.toml 存在），一个用例都跑
    不起来时，「你的仓库全绿」是一句比中止更糟的答复。

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
        # **超时要单独说**。`ExecResult.timed_out` 这个字段一直都在，只是没人读
        # —— 实测（2026-07-30，轮 9）拿 aifix 自己当目标跑，套件在 worktree 里
        # 跑满 900 秒被杀，而消息把「超时 / 崩溃 / 沙箱失败」三种揉成一句，
        # 不报数字、不指旋钮，唯一的成因却恰恰是那个写死的 900。
        #
        # 三种原因的下一步完全不同：超时要调旋钮或看套件为什么慢，崩溃要看
        # 目标项目，沙箱失败要看 aifix 自己。给错方向的提示比不给更费时间。
        if getattr(res, "timed_out", False):
            raise RuntimeError(
                f"跑目标项目的测试**超时**了（>{timeout}s，worktree={worktree}）。\n"
                f"  这不是模型的问题，也不是目标项目有 bug —— 是这次给它的时间"
                f"不够跑完。\n"
                f"  下一步：调大 `AIFIX_TEST_TIMEOUT_SECONDS`"
                f"（局部重跑是 `AIFIX_SCOPED_TEST_TIMEOUT_SECONDS`），"
                f"或者先弄清目标项目的套件为什么这么慢。")
        raise RuntimeError(
            f"测试未产出任何 JUnit 报告（worktree={worktree}）："
            "测试进程没能正常跑完（崩溃 / 沙箱执行失败），本次结果不可信")
    if required and not ran:
        raise RuntimeError(
            f"测试报告里一个用例都没跑（worktree={worktree}）："
            "进程正常退出了，是**收集**没成功（node id 无效 / conftest 抛异常 / "
            "测试依赖缺失），本次结果不可信")


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
                         timeout: float = 1800.0,
                         require_report: bool = False):
    """在 worktree 里跑全量测试并解析报告。零 LLM。

    require_report 默认 False，但核心循环的每一个调用点都显式传了 True
    （见 _check_report）：默认值留给「少一份报告确实无所谓」的调用方，
    产品路径上不存在这样的调用方。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        # 剥掉 aifix 自己的 AIFIX_* 变量：它们会被目标项目读走
        # （见 aifix.testenv 里那段实测）
        res = await sb.exec(sanitized_command(adapter.full_test_command()),
                            timeout)
        paths = adapter.report_paths(worktree)
        # 先解析再检查：「跑了几个用例」只有报告内容知道，文件在不在答不了
        fs = parse_junit(paths, adapter.make_test_id)
        _check_report(worktree, paths, fs.ran, require_report, res, timeout)
        return fs
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=False)
        await sb.close()


async def run_scoped(worktree: Path, adapter: ProjectAdapter,
                     test_ids: list[str], timeout: float = 600.0,
                     require_report: bool = False):
    """只跑指定用例并解析报告。供 flaky 确认使用 —— 成本远低于全量。

    走 scoped 那份报告：调用它的时候全量那份通常还要继续用，不能被覆盖。
    """
    sb = LocalSandbox(workspace=str(worktree))
    await sb.start()
    try:
        res = await sb.exec(
            sanitized_command(adapter.scoped_test_command(test_ids)),
            timeout)
        paths = adapter.report_paths(worktree, scoped=True)
        fs = parse_junit(paths, adapter.make_test_id)
        _check_report(worktree, paths, fs.ran, require_report, res, timeout)
        return fs
    finally:
        await _rm_reports(sb, adapter, worktree, scoped=True)
        await sb.close()


async def baseline_node(state: AifixState) -> dict[str, Any]:
    """跑一次全量，同时产出 id 列表与 Failure 对象——全量测试很贵，只跑这一次。

    `require_report=True`：baseline 是整个 run 唯一一次「改动之前长什么样」
    的测量，没有下一轮可以兜底。测试进程没跑成时返回空集合，队列就是空的，
    run 以「修复 0 / 0、全绿、没活干」正常收场、退出码 0 —— 用户得到的是一句
    「你的仓库没问题」，而真相是测试压根没跑起来。

    `require_report=True` 只拦得住「一份报告都没写出来」。报告写出来了、里面
    却全是收集错误，是另一条缝，由 `collection_error_abort` 拦（见那里）。

    `baseline_ids` 在中止分支里**照旧返回**：它是这一跑的真实测量结果，报告
    与 trace 都不该因为我们不信任它就假装没看见。不可信的是「拿它当工单」这个
    动作，所以被清空的是 queue。
    """
    adapter = adapter_from_state(state)
    warn_if_patch_may_be_invisible(state, adapter)
    fs = await run_full_suite(Path(state["worktree_path"]), adapter,
                              timeout=state["config"].test_timeout_seconds,
                              require_report=True)
    ids = sorted(fs.ids)
    # _ran 是**跑出结果的用例总数**（通过 + 失败，不含 skipped），只供进度
    # 显示。红的数目单独看分不出 2/14 还是 2/2000，而后者意味着后面每轮
    # verify 都要再跑一次那 2000 个 —— 用户看到的停顿会长得多，且那是正常的。
    out: dict[str, Any] = {"baseline_ids": ids, "queue": list(ids),
                           "_failures": dict(fs.failures), "_ran": len(fs.ran),
                           "abort": None, "abort_kind": None}
    bad = collection_error_abort(ids, adapter)
    if bad is None:
        return out
    trace = trace_of(state)
    if state["config"].allow_collection_errors:
        # 逃生口开着也要留痕：一条被静默绕过的守卫等于没有守卫。写 stderr 是
        # 因为报告要等整个 run 跑完才渲染，而这句话得在花钱之前说出来。
        trace.fact("collection_errors_allowed",
                   len(file_level_ids(ids, adapter)))
        print(f"⚠️  警告：{bad}\n"
              "    AIFIX_ALLOW_COLLECTION_ERRORS 已开启，"
              "这批 id 会照常排队开修。", file=sys.stderr, flush=True)
        return out
    # 事实自己记：run_once 只在预算 / 崩溃两条路上记 abort，这条中止发生在
    # 那之前。replay 与 trajectory 读的是事实，不是渲染出来的报告。
    trace.fact("abort", bad)
    trace.fact("abort_kind", COLLECTION_ABORT_KIND)
    return {**out, "queue": [], "abort": bad,
            "abort_kind": COLLECTION_ABORT_KIND}
