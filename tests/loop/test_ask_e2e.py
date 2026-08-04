"""停下来问人，拿到答复之后重新跑 —— 命令行这一路的完整闭环。

用脚本化模型替身，不打网络。

这条路最容易出的错不是崩溃，是**静默地把「悬而未决」当成「试过了不行」**：
问题被埋进报告底部、pending 没落盘、或者答复没真正进到模型的开场白里。
下面每一条都对着其中一种。
"""
import json
import subprocess

import pytest

from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.runtime.pending import PENDING_FILE, choose, latest, load
from tests.loop.test_e2e import _Scripted, _text, _tool


def _names(tools):
    """框架传给 client 的 `tools` 是 **schema 字典**，不是工具对象。

    写成 `t.name` 会抛 AttributeError，而它发生在一个 async generator 里 ——
    被吞成「这一轮模型调用失败」，断言看到的是 KeyError 而不是真因。
    """
    return sorted(t["function"]["name"] for t in tools)

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "空输入没有约定行为", "fix_strategy": "先问清楚",
    "confidence": "low",
})

_Q = "add() 收到 None 时应该怎么办？"
_OPTS = ["抛 TypeError", "当作 0 处理"]


def _asking_fixer(extra=()):
    """先读一次代码（否则 ask_user 会被第一道约束拦死），再提问。"""
    return _Scripted([
        _tool("read_file", json.dumps({"path": "calc.py"})),
        _tool("ask_user", json.dumps({"question": _Q, "options": _OPTS})),
        *extra,
        _text("等你回答"),
    ])


async def _ask_run(repo, run_id="ask1", config=None):
    return await run_once(repo, config or AifixConfig(), run_id=run_id,
                          detector_client=_Scripted([_text(_DIAG)]),
                          fixer_client=_asking_fixer())


async def test_the_run_stops_and_says_it_needs_input(buggy_repo):
    """核心：提问之后这次 run 以 `needs_input` 收尾，而不是「没修好」。

    两者对人的含义完全相反 —— 一个是「该你了」，一个是「它不行」。
    """
    state = await _ask_run(buggy_repo)
    assert state["abort_kind"] == "needs_input"
    assert state["ask"]["question"] == _Q
    assert state["ask"]["options"] == _OPTS


async def test_the_question_is_at_the_top_of_the_report(buggy_repo):
    """问题要排在成绩单**之前**：这次 run 的产出就是这个问题，
    塞在表格底下等于让人自己去找。"""
    state = await _ask_run(buggy_repo)
    md = state["report_md"]
    assert _Q in md
    assert md.index(_Q) < md.index("| 测试用例 |")
    # 怎么回答也要写全 —— 一个「我需要更多信息」却不说怎么给的提示是死路
    assert "aifix answer" in md and "/aifix" in md


async def test_the_pending_question_survives_the_run(buggy_repo):
    """落盘是必需的：`aifix answer` 在**另一次进程**里跑，而 worktree
    退出时连同里面的一切都被删了。"""
    state = await _ask_run(buggy_repo)
    data = load(buggy_repo, "ask1")
    assert data is not None, "pending.json 没落盘"
    assert data["question"] == _Q
    assert data["test_id"] == state["ask"]["test_id"]
    # repo 必须带上：另一次进程要知道回哪个仓库重跑
    assert data["repo"] == str(buggy_repo)
    assert latest(buggy_repo)["question"] == _Q


async def test_nothing_is_delivered_while_the_question_is_open(buggy_repo):
    """**提问即回滚。**

    模型是在声明「我不知道什么才算对」的同一轮里做的改动，没有任何人看过
    它们。让它们进 verify 有可能被判 BETTER 而直接交付 —— 那等于用「测试
    通过」回答了一个测试答不了的问题。
    """
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")
    fixer = _Scripted([
        _tool("read_file", json.dumps({"path": "calc.py"})),
        # 一边提问一边偷偷改：必须被回滚
        _tool("edit_file", json.dumps({
            "path": "calc.py", "old_text": "return a - b",
            "new_text": "return a + b"})),
        _tool("ask_user", json.dumps({"question": _Q, "options": _OPTS})),
        _text("等你回答"),
    ])
    state = await run_once(buggy_repo, AifixConfig(), run_id="ask2",
                           detector_client=_Scripted([_text(_DIAG)]),
                           fixer_client=fixer)
    assert state["abort_kind"] == "needs_input"
    assert state["touched"] == [], state["touched"]
    assert state["results"] == [], "悬而未决不该被记成一次失败的尝试"
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original
    # 比整份内容，不比子串：源文件里那句注释就写着 `# bug: 应为 a + b`，
    # 拿 "a + b" 去 in 判定的话，这条断言在修好和没修好时都成立。
    show = subprocess.run(["git", "show", "aifix/ask2:calc.py"],
                          cwd=buggy_repo, capture_output=True, text=True)
    assert show.stdout == original or show.returncode != 0, show.stdout


async def test_an_empty_diff_guard_does_not_hijack_the_question(buggy_repo):
    """一字未改正是提问之后的**正常**结果。

    空 diff 那道守卫会带着「你没有做出任何修改」的反馈一路重试到 giveup ——
    模型每一轮都做对了，额度却烧光，而报告只会说 `empty_diff_giveup`，
    问题连提都不会被提。所以提问的判定必须排在那道守卫**之前**。
    """
    state = await _ask_run(buggy_repo, run_id="ask3")
    assert state["abort_reason"] == "needs_input"
    assert "empty_diff" not in (state.get("guard_hits") or [])


async def test_the_answer_reaches_the_model_and_the_fix_lands(buggy_repo):
    """闭环的另一半：带着答复重跑，这次真修好。"""
    from aifix.agents.fixer import format_answer

    await _ask_run(buggy_repo, run_id="ask4")
    data = load(buggy_repo, "ask4")
    picked = choose(data, 2)
    assert picked == "当作 0 处理"

    seen: dict[str, str] = {}

    class _Recording(_Scripted):
        async def stream(self, messages, tools):
            seen.setdefault("first", messages[-1].content)
            seen["tools"] = ",".join(_names(tools))
            async for c in super().stream(messages, tools):
                yield c

    fixer = _Recording([
        _tool("edit_file", json.dumps({
            "path": "calc.py", "old_text": "return a - b",
            "new_text": "return a + b"})),
        _text("已修复"),
    ])
    state = await run_once(
        buggy_repo, AifixConfig(), run_id="ask5",
        only_test=data["test_id"],
        answer=format_answer(data["question"], picked),
        detector_client=_Scripted([_text(_DIAG)]), fixer_client=fixer)

    # 答复真的进了开场白 —— 只落盘不喂给模型是这条路最安静的失败方式
    assert picked in seen["first"], seen["first"]
    # 而且**不再给它 ask_user**：答案就在眼前，再问一次同样的问题最贵
    assert "ask_user" not in seen["tools"], seen["tools"]
    assert [r["verdict"] for r in state["results"]] == ["better"]


async def test_eval_never_offers_a_tool_nobody_can_answer(buggy_repo):
    """`ask_user` 只在**有人能回答**的场合注册。

    评测并行跑几十个任务、没有任何人在看。留着它等于给模型一条烧钱的岔路，
    而那个失分会被记到模型头上，实际是评测环境的账。
    """
    seen: dict[str, str] = {}

    class _Recording(_Scripted):
        async def stream(self, messages, tools):
            seen["tools"] = ",".join(_names(tools))
            async for c in super().stream(messages, tools):
                yield c

    cfg = AifixConfig(ask_user=False)
    state = await run_once(buggy_repo, cfg, run_id="ask6",
                           detector_client=_Scripted([_text(_DIAG)]),
                           fixer_client=_Recording([_text("没工具可用")]))
    assert "ask_user" not in seen["tools"], seen["tools"]
    assert state.get("ask") is None
    assert not (buggy_repo / ".aifix" / "runs" / "ask6" / PENDING_FILE).exists()


@pytest.mark.parametrize("bad", [0, -1, 3, 99])
def test_an_out_of_range_choice_is_refused(bad):
    """编号越界必须当场拒。

    放过去的话它会**静静地按另一个选项去改代码**，而人以为自己选的是刚才
    屏幕上那一条 —— 不崩溃、不报错，只是改错了地方。
    """
    with pytest.raises(ValueError, match="超出范围"):
        choose({"options": _OPTS}, bad)


def test_the_numbering_starts_at_one():
    """屏幕上印的是「1. / 2.」，人回的是那个数字。这里差一位不会崩，
    只会按另一个选项去改。"""
    assert choose({"options": _OPTS}, 1) == _OPTS[0]
    assert choose({"options": _OPTS}, 2) == _OPTS[1]
