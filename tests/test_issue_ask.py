"""issue 那一路的问答闭环：提问回帖 → `/aifix <编号>` → 带着答复重跑。

这条路和命令行那条的**语义必须一样**（都是重跑，不是断点恢复），但持久层
完全不同：Actions 的 job 一次性，容器连同磁盘一起消失，`.aifix/` 下的任何
东西都活不过一次 job。**issue 评论才是这条流水线唯一活得下来的存储。**

由此推出一条不显然但要命的要求：**复现测试也得跟着问题一起存进评论**。
下一个 job 是一个干净的 checkout，上一次写下去的那个文件不存在了；不带它
的话只能重跑一次 reproducer，而它未必写出同一条测试 —— 人回答的却是针对
上一条测试的问题。
"""
import copy
import json
from pathlib import Path

import pytest

from aifix import pending as pending_store
from aifix.agents.reproducer import Reproduction
from aifix.config import AifixConfig
from aifix.issue.event import authorize
from aifix.issue.handle import handle
from aifix.reproduce import ReproduceOutcome
from tests.test_issue_handle import _Gh, _payload, _REPRO, _state

_ASK = {"test_id": "tests/test_issue_1.py::test_x",
        "question": "空购物车应该返回什么？",
        "options": ["返回 None", "抛 ValueError"]}


# ---------------------------------------------------------------- 授权

@pytest.mark.parametrize("body,expect", [
    ("/aifix", None), ("/aifix 1", 1), ("/aifix 12", 12),
    ("/aifix  3", 3), ("/aifix 2\r\n随便说两句", 2),
])
def test_the_answer_form_is_recognised(body, expect):
    d = authorize(_payload(body))
    assert d.allowed, d.reason
    assert d.event.answer_choice == expect


@pytest.mark.parametrize("body", ["/aifix 一", "/aifix -1", "/aifixx 1",
                                  "/aifix 1 2", "看看 /aifix 1"])
def test_things_that_only_look_like_an_answer_are_ignored(body):
    """宽松匹配的代价是把闲聊当成命令。`/aifix -1` 尤其要挡：负数会绕过
    「从 1 数起」的直觉，一路走到 choose 才被拦。"""
    assert not authorize(_payload(body)).allowed


def test_answering_needs_the_same_permission_as_asking():
    """**回答一个问题会直接决定代码怎么改**，它和发起一次修复是同一级别的
    动作。给它一条更宽的门，等于把整套权限判定绕过去。"""
    p = _payload("/aifix 1")
    p["comment"]["author_association"] = "CONTRIBUTOR"
    d = authorize(p)
    assert not d.allowed and d.notify


# ---------------------------------------------------------------- 标记

def test_the_marker_survives_a_question_containing_comment_syntax():
    """问题正文是模型写的自由文本，里面完全可能出现 `-->`。

    裸 JSON 会被当场截断 —— 后半截直接显示在 issue 上，标记再也解析不出来。
    这不是理论风险：让模型描述一个跟注释语法有关的缺陷，它就会写出来。
    """
    nasty = {**_ASK, "question": "为什么 `<!-- x -->` 会被吃掉？"}
    body = "前面一段\n\n" + pending_store.encode_marker(nasty) + "\n\n后面一段"
    assert pending_store.decode_marker(body) == nasty


def test_a_comment_without_a_marker_reads_as_no_pending():
    """没有问题在等是**正常状态**，不是错误。"""
    assert pending_store.decode_marker("普通的一条评论") is None
    assert pending_store.decode_marker("") is None
    # 坏掉的标记也一样：宁可当作没有，也不要拿半份数据去改代码
    assert pending_store.decode_marker("<!-- aifix:ask 这不是base64 -->") is None


# ---------------------------------------------------------------- 编排

async def _run(gh, body="/aifix", state=None, status_body="", tmp_path=None,
               capture=None):
    async def _repro(*a, **k):
        return ReproduceOutcome(_REPRO)

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        if capture is not None:
            capture.update(kw)
        return state if state is not None else _state()

    # 得先让 detect_adapter 认得出这是个 pytest 项目，否则整条编排在第一步
    # 就以 no_repro 返回，下面每条断言测的都是同一件不相干的事
    (tmp_path / "tests").mkdir(exist_ok=True)
    gh.status_body = lambda issue: status_body
    return await handle(
        _payload(body), tmp_path, AifixConfig(), gh,
        reproduce_fn=_repro, red_check_fn=_red, run_fn=_run_fn,
        publish=lambda *a, **k: True,
        git=lambda *a, **k: "")


async def test_a_pending_question_is_posted_instead_of_a_pr(tmp_path):
    """**排在建 PR 之前。**

    那一轮的改动已经被回滚了，交付分支上除了复现测试什么都没有。给它开一个
    PR 等于请人 review 一个空改动，而真正要人做的事会被埋进 PR 描述里。
    """
    gh = _Gh()
    res = await _run(gh, state=_state(ask=_ASK, results=[],
                                      abort_kind="needs_input"),
                     tmp_path=tmp_path)
    assert res.path == "needs_input"
    assert gh.prs == [], "等人回答时不该开 PR"
    body = gh.statuses[-1][1]
    assert _ASK["question"] in body
    assert "1. 返回 None" in body and "2. 抛 ValueError" in body
    assert "/aifix <编号>" in body


async def test_the_reproduction_rides_along_in_the_marker(tmp_path):
    """下一个 job 是干净的 checkout —— 复现测试不跟着存，答复回来时就没了。"""
    gh = _Gh()
    await _run(gh, state=_state(ask=_ASK, results=[],
                                abort_kind="needs_input"), tmp_path=tmp_path)
    data = pending_store.decode_marker(gh.statuses[-1][1])
    assert data["question"] == _ASK["question"]
    assert data["repro"]["test_code"] == _REPRO.test_code
    assert data["repro"]["target_test_id"] == _REPRO.target_test_id


async def test_the_answer_is_carried_into_the_rerun(tmp_path):
    """闭环：`/aifix 2` → 选项原文进到 run_once 的 answer 里。"""
    gh = _Gh()
    marker = pending_store.encode_marker({
        **_ASK, "run_id": "old", "repo": str(tmp_path),
        "repro": {"test_file": _REPRO.test_file,
                  "test_code": _REPRO.test_code,
                  "target_test_id": _REPRO.target_test_id}})
    seen: dict = {}
    await _run(gh, body="/aifix 2", status_body=f"状态\n\n{marker}",
               tmp_path=tmp_path, capture=seen)
    assert "抛 ValueError" in (seen.get("answer") or ""), seen
    # 只跑当初卡住的那条，不是整份 baseline
    assert seen["only_test"] == _REPRO.target_test_id


async def test_answering_does_not_burn_a_model_call_on_reproduction(tmp_path):
    """答复这一路**不重跑 reproducer**。

    那不但要再花一次模型调用，而且它未必写出同一条测试 —— 人回答的却是针对
    上一条测试的问题，换一条就答非所问。
    """
    gh = _Gh()
    called = []

    async def _repro(*a, **k):
        called.append(1)
        return ReproduceOutcome(_REPRO)

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        return _state()

    marker = pending_store.encode_marker({
        **_ASK, "repro": {"test_file": _REPRO.test_file,
                          "test_code": _REPRO.test_code,
                          "target_test_id": _REPRO.target_test_id}})
    (tmp_path / "tests").mkdir(exist_ok=True)
    gh.status_body = lambda issue: marker
    await handle(_payload("/aifix 1"), tmp_path, AifixConfig(), gh,
                 reproduce_fn=_repro, red_check_fn=_red, run_fn=_run_fn,
                 publish=lambda *a, **k: True, git=lambda *a, **k: "")
    assert called == [], "答复这一路不该再调 reproducer"


async def test_answering_when_nothing_is_pending_says_so(tmp_path):
    """必须出声。静默丢弃会让人以为它已经在跑了 —— 「不报错、只有承诺是
    假的」正是这个项目栽过十次以上的那种失败。"""
    gh = _Gh()
    res = await _run(gh, body="/aifix 1", status_body="没有标记的一条评论",
                     tmp_path=tmp_path)
    assert res.path == "no_pending"
    assert "没有待回答的问题" in gh.comments[-1][1]
    assert gh.prs == []


async def test_a_marker_without_the_reproduction_says_so(tmp_path):
    """标记里没带复现测试（旧版本留下的、或正文被截断）。

    裸构造 Reproduction 的话 test_file 是 None，会一路走到 write_reproduction
    才以 TypeError 炸掉 —— 那时 issue 上没有任何说明，人只能去 Actions 页面
    读一段调用栈。
    """
    gh = _Gh()
    marker = pending_store.encode_marker({**_ASK, "repro": {}})
    res = await _run(gh, body="/aifix 1", status_body=marker,
                     tmp_path=tmp_path)
    assert res.path == "no_pending"
    assert "没有复现测试" in gh.comments[-1][1]


@pytest.mark.parametrize("n", [0, 3, 99])
async def test_an_out_of_range_choice_is_refused_and_explained(tmp_path, n):
    """越界当场拒。放过去的话它会静静地按另一个选项去改代码，
    而人以为自己选的是评论里的那一条。

    **排在「有没有复现测试」之前**：编号错是人的笔误，该原样告诉他；
    先报「没有复现测试」等于用一个技术细节盖住了真正的问题。
    """
    gh = _Gh()
    marker = pending_store.encode_marker({**_ASK, "repro": {}})
    res = await _run(gh, body=f"/aifix {n}", status_body=marker,
                     tmp_path=tmp_path)
    assert res.path == "bad_choice"
    assert "超出范围" in gh.comments[-1][1]
    assert gh.prs == []
