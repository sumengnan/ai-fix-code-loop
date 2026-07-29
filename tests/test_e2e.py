"""端到端验收：红色测试进去，绿色分支出来，主工作区未被触碰。

用脚本化模型替身，不打网络。
"""
import json
import subprocess

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


async def test_red_in_green_out(buggy_repo):
    detector = _Scripted([_text(_DIAG)])
    fixer = _Scripted([
        _tool("apply_patch", json.dumps({"diff": _PATCH})),
        _text("已修复"),
    ])
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")

    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e",
                           detector_client=detector, fixer_client=fixer)

    # 1. 目标用例被判定为修复
    assert [r["verdict"] for r in state["results"]] == ["better"]
    # 2. 主工作区一字未动
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original
    # 3. 修复落在独立分支上
    show = subprocess.run(["git", "show", "aifix/e2e:calc.py"],
                          cwd=buggy_repo, capture_output=True, text=True)
    assert "a + b" in show.stdout
    # 4. 报告可读
    assert "1 / 1" in state["report_md"]


async def test_green_repo_reports_no_work(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e2",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["results"] == []
    assert "0 / 0" in state["report_md"]


async def test_dirty_repo_aborts(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    state = await run_once(buggy_repo, AifixConfig(), run_id="e2e3",
                           detector_client=_Scripted([_text("x")]),
                           fixer_client=_Scripted([_text("x")]))
    assert state["abort"] is not None
    assert "工作区不干净" in state["report_md"]


class _Recording:
    """记下 run_once 报了哪些进度，不渲染。

    断言的是**调用**而不是渲染出来的文字：措辞归 TerminalProgress 管
    （tests/test_progress.py 钉），这里管的是核心循环有没有在该出声的地方
    出声 —— 两者分开，改一句措辞不会弄红这条集成测试。
    """

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def rec(**kw):
            self.calls.append((name, kw))
        return rec

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


async def test_run_once_reports_progress_through_the_whole_loop(buggy_repo):
    """一次完整的 run 里，四个阶段都要出声。

    此前 `aifix run` 从头到尾一个字不印，5 分钟的等待里分不出「在干活」和
    「卡死了」。这条钉的是接线本身：baseline、每个 failure 的开头、诊断、
    验证、收尾，一个都不能少。
    """
    prog = _Recording()
    await run_once(buggy_repo, AifixConfig(), run_id="prog1",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")]),
                   progress=prog)

    kinds = prog.kinds()
    for expected in ("run_start", "baseline", "failure_start", "detected",
                     "verified", "finished"):
        assert expected in kinds, f"{expected} 没有出声：{kinds}"
    # 顺序：先说清这是哪次 run，再报规模，最后才轮到具体 failure
    assert kinds.index("run_start") < kinds.index("baseline") \
        < kinds.index("failure_start") < kinds.index("verified")

    # 报的数得是真的：buggy_repo 是 2 个用例、1 个红的
    base = dict(prog.calls[kinds.index("baseline")][1])
    assert base["ran"] == 2 and base["failing"] == 1
    head = dict(prog.calls[kinds.index("failure_start")][1])
    assert head == {"index": 1, "total": 1,
                    "test_id": "tests/test_calc.py::test_add"}
    assert dict(prog.calls[kinds.index("verified")][1])["verdict"] == "better"


async def test_run_once_is_silent_by_default(buggy_repo, capsys):
    """不传 progress 时一个字都不许印。

    `eval` 会并行跑几十个任务，每个都是一次完整的 run —— 默认出声的话几十条
    进度交织成一团，eval 自己那行 `✅ task_id` 反而被淹掉。
    """
    await run_once(buggy_repo, AifixConfig(), run_id="prog2",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")]))
    out, err = capsys.readouterr()
    assert out == "" and err == "", f"默认不该出声：{out!r} / {err!r}"


async def test_the_fix_phase_emits_a_real_heartbeat(buggy_repo):
    """心跳必须由**真的 AgentLoop** 产出的事件驱动。

    这条测试存在的理由是它抓到过一个真 bug：`consume` 原本监听
    `harness.events.ToolCall`，而那是消息里的数据结构，**从不作为事件出现在
    流上**（实测一次真 run：ToolStarted 33 条、ToolFinished 33 条、
    ToolCallRequested 30 条、ToolCall 0 条）。单元测试自己构造 ToolCall 喂进
    consume，测试全绿，真跑一步都不报 —— 与 `locate_source` 那次是同一个错误
    形状：**测试喂了系统从不产出的输入**。

    所以这里不构造任何事件，走 `run_once` 的完整链路，让脚本化模型替身经过
    真的 AgentLoop、真的工具注册表。事件类型再改名，这条会红。
    """
    prog = _Recording()
    await run_once(buggy_repo, AifixConfig(), run_id="hb1",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")]),
                   progress=prog)

    steps = [kw for k, kw in prog.calls if k == "agent_step"]
    assert steps, f"fix 阶段一步都没报心跳：{prog.kinds()}"
    assert steps[0]["tool"] == "apply_patch"
    # 步号从 1 起、连续递增：屏幕上出现两段「第 1 步」会读成重新开始了
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    # 参数要带上：光有工具名分不出它在改哪个文件
    assert "diff" in steps[0]["arguments"]

    # 成败同理，而且只有 ToolFinished 知道 —— 这一条与上面是同一个错误形状的
    # 两侧：监听错了事件类型的话，单元测试照样绿，真跑时屏幕上永远没有勾也
    # 没有叉，成功和失败长得一模一样。
    done = [kw for k, kw in prog.calls if k == "agent_step_done"]
    assert done, f"没有一条工具结果被报出来：{prog.kinds()}"
    assert done[0]["tool"] == "apply_patch" and done[0]["ok"] is True
    assert done[0]["result"], "结果正文要带上，失败时它就是原因"
    # 结束那条复用自己那次调用的步号，不是另起一套编号
    assert [d["step"] for d in done] == [s["step"] for s in steps]
