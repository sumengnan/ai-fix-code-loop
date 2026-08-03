"""Detector 判断「根本原因是什么」时，得**看得见那段代码**。

在 snippet.py 之前它看到的只有路径、行号和 traceback —— 无工具、单步，那段
源码它从未见过。于是 `suspect_lines` 只能编，而编出来的行号会原样进入 Fixer
的开场白（「嫌疑行号：120-135」），把它的第一步引向一个具体而错误的位置。

代码就在磁盘上。读它不需要模型、不多花一个回合、不花一分钱。
"""
import pytest

from aifix.adapters.base import Failure, SourceCandidate
from aifix.agents.detector import build_prompt
from aifix.snippet import around

_SRC = "\n".join(f"line {i}" for i in range(1, 101)) + "\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(_SRC, encoding="utf-8")
    return tmp_path


def _cand(line=50, frame="f"):
    return SourceCandidate(path="src/m.py", line=line, frame=frame)


def test_the_real_source_comes_back_with_real_line_numbers(repo):
    """行号必须是**文件里的**行号。

    模型拿它填 suspect_lines，Fixer 再拿那个去定位 —— 从 1 重新编号会让
    整条链指向错误的位置，而且错得很安静。
    """
    out = around(repo, "src/m.py", 50, radius=2)
    assert "    50\tline 50" in out
    assert "line 48" in out and "line 52" in out
    assert "line 47" not in out and "line 53" not in out


def test_the_frame_line_is_marked(repo):
    """一屏二十几行里，模型要知道 traceback 指的是哪一行。
    不标它就得自己数，而数数正是它最不擅长的事。"""
    out = around(repo, "src/m.py", 50, radius=2)
    marked = [ln for ln in out.splitlines() if ln.startswith(">")]
    assert len(marked) == 1 and "line 50" in marked[0]


def test_the_window_clamps_at_both_ends(repo):
    """靠近文件头尾时不能越界 —— 这里崩掉会让整个 detect 步骤失败，
    而它本来只是个锦上添花的输入。"""
    assert "line 1" in around(repo, "src/m.py", 1)
    assert "line 100" in around(repo, "src/m.py", 100)


def test_an_unreadable_path_degrades_to_none(repo):
    """读不到就不给 —— 缺一段源码只是少了点上下文，抛异常会让整步失败。"""
    assert around(repo, "src/nope.py", 5) is None
    assert around(repo, "src", 5) is None                  # 目录
    assert around(repo, "../../etc/passwd", 1) is None     # 逃逸


def _failure():
    return Failure(test_id="tests/test_m.py::test_x", classname="tests.test_m",
                   name="test_x", message="assert 1 == 2",
                   trace="  File \"src/m.py\", line 50, in f\n")


def test_the_prompt_carries_the_source_under_its_candidate(repo):
    """源码要挂在**对应的**候选下面，不能堆在末尾 —— 候选有好几个，
    堆在一起模型就得自己配对，而配错的代价是诊断指向错误的文件。"""
    cands = [_cand()]
    snip = {0: around(repo, "src/m.py", 50, radius=1)}
    text = build_prompt(_failure(), cands, snip)
    head = text.index("1. src/m.py:50")
    assert head < text.index("line 50") < text.index("完整 traceback")


def test_without_source_the_model_is_told_not_to_invent_line_numbers(repo):
    """没有源码时必须明说「填 null」。

    不说的话模型会照样编一个 —— 它一直是这么做的，因为格式里有这个字段。
    编出来的行号比不给更糟：不给是「不知道」，编一个是「指错地方」。
    """
    text = build_prompt(_failure(), [_cand(frame=None)])
    assert "null" in text


def test_with_source_the_model_is_told_to_stay_inside_it(repo):
    cands = [_cand(frame=None)]
    text = build_prompt(_failure(), cands,
                        {0: around(repo, "src/m.py", 50)})
    assert "真实存在" in text and "null" not in text


def test_detect_node_actually_reads_the_files(repo, monkeypatch):
    """**接线检查**：函数写好了但没人调用，是这个项目反复吃过的亏。

    这里不 mock build_prompt，而是看真正发到模型那一侧的 prompt 里有没有
    源码 —— 那才是「Detector 看得见代码」这句话的唯一证据。
    """
    import asyncio

    from aifix.nodes import detect as detect_mod

    seen: dict[str, str] = {}

    class _Loop:
        def __init__(self, **kw):
            pass

        def run(self, prompt):
            seen["prompt"] = prompt

            async def _gen():
                return
                yield
            return _gen()

    class _Outcome:
        ok, text, events, tokens, cost_usd = False, "", [], 0, 0.0
        event_times: list = []

    async def _consume(_gen, **kw):
        return _Outcome()

    class _Adapter:
        def test_dirs(self):
            return ["tests"]

        def locate_source(self, failure, repo_):
            return [_cand()]

    monkeypatch.setattr(detect_mod, "AgentLoop", _Loop)
    monkeypatch.setattr(detect_mod, "consume", _consume)
    monkeypatch.setattr(detect_mod, "adapter_from_state", lambda s: _Adapter())

    from aifix.config import AifixConfig

    # 不塞 _trace：graph.trace_of 在缺席时返回吞掉一切的空实现，这一条
    # 测试要看的是 prompt，不是落盘。
    state = {
        "config": AifixConfig(),
        "_failures": [_failure()],
        "current": 0,
        "worktree_path": str(repo),
        "spent_tokens": 0,
        "spent_usd": 0.0,
    }
    asyncio.run(detect_mod.detect_node(state, client=object()))
    assert "line 50" in seen["prompt"], seen["prompt"][:400]
