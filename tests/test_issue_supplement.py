"""`/aifix <文字>`：补充说明，以及用自己的话回答提问。

命令语法只有两个形态（`/aifix` 和 `/aifix <文字>`），**那段文字是什么意思由状态
决定**：issue 上挂着待答问题就是回答，否则就是对本次缺陷的补充说明。

这个取舍换来的是「不会因为将来新增命令而改变已有写法的含义」，代价是同一句话
在两种状态下含义不同 —— 所以这里每一条都要顺带钉住**机器有没有回显它采纳的是
哪种解读**。静默采纳一种解读是这个项目最忌讳的形态。
"""
import copy

import pytest

from aifix import pending as pending_store
from aifix.agents.reproducer import Reproduction
from aifix.config import AifixConfig
from aifix.issue.event import authorize
from aifix.issue.handle import handle
from aifix.reproduce import ReproduceOutcome
from tests.test_issue_handle import _Gh, _payload, _state

_ASK = {"test_id": "tests/test_issue_1.py::test_x",
        "question": "空购物车应该返回什么？",
        "options": ["返回 None", "抛 ValueError"]}


def _repro():
    """**每次造一个新的。**

    `write_reproduction` 撞名时会**原地改** `test_file`（改名落地，绝不覆盖
    仓库里已有的测试）。共用一个模块级实例的话，跑过一次之后它就变成了
    `..._aifix.py`，而这个污染会顺着 import 漏进别的测试文件 —— 表现是那边
    突然找不到自己刚写下去的文件。
    """
    return Reproduction(
        can_reproduce=True, test_file="tests/test_issue_1.py",
        test_code="def test_x():\n    assert False\n",
        target_test_id="tests/test_issue_1.py::test_x")


def _ask_marker():
    """一条带着复现测试的待答标记 —— 缺了复现测试那条路会提前返回。"""
    r = _repro()
    return pending_store.encode_marker({
        **_ASK, "repro": {"test_file": r.test_file,
                          "test_code": r.test_code,
                          "target_test_id": r.target_test_id}})


async def _run(gh, body, tmp_path, status_body=None, seen=None):
    """跑一次 handle，把喂给复现器与 fixer 的东西记进 `seen`。

    `status_body=None` 表示**让替身自己回读它写过的状态评论** —— 跨轮的状态
    就是这么活下来的，写死一个字符串会把这条链子测没了。
    """
    seen = seen if seen is not None else {}

    async def _reproduce_fn(repo, adapter, config, title, issue_body):
        seen["title"], seen["body"] = title, issue_body
        return ReproduceOutcome(_repro())

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        seen["answer"] = kw.get("answer")
        return _state()

    (tmp_path / "tests").mkdir(exist_ok=True)
    if status_body is not None:
        gh.status_body = lambda issue: status_body
    res = await handle(_payload(body), tmp_path, AifixConfig(), gh,
                       reproduce_fn=_reproduce_fn, red_check_fn=_red, run_fn=_run_fn,
                       publish=lambda *a, **k: True, git=lambda *a, **k: "")
    return res, seen


# ------------------------------------------------------------ 补充说明

async def test_free_text_becomes_a_supplement_when_nothing_is_pending(tmp_path):
    """没有待答问题时，`/aifix <文字>` 是对本次缺陷的补充。

    issue 的标题和正文常常写不全，而在评论里补一句是人最自然的动作 —— 从前
    这条路是堵死的（当场拒绝），唯一的出路是重开一个 issue。
    """
    gh = _Gh()
    _, seen = await _run(gh, "/aifix 只有购物车为空时才崩", tmp_path)
    assert "只有购物车为空时才崩" in seen["body"]
    # issue 正文照旧带着 —— 补充是**叠加**，不是替换
    assert "Trivy" in seen["body"], seen["body"][:200]


async def test_the_supplement_is_echoed_back(tmp_path):
    """必须回显采纳的是哪种解读。

    同一句话在有无待答问题两种状态下含义不同，不回显的话人无从知道机器把它
    当成了什么。
    """
    gh = _Gh()
    await _run(gh, "/aifix 只有购物车为空时才崩", tmp_path)
    said = "\n".join(b for _, b in gh.statuses)
    assert "补充说明" in said and "只有购物车为空时才崩" in said


async def test_a_supplement_survives_into_the_next_bare_command(tmp_path):
    """**跨轮活下来**：`/aifix <补充>` 之后，光 `/aifix` 重跑的是那份补充。

    Actions 的 job 一次性，容器连同磁盘一起消失 —— 不把它存进状态评论的话，
    第二轮只能退回去读 issue 正文，也就是**重跑了另一件事**，而表面上完全
    看不出来。

    中间隔着好几次 `upsert_status`（收到补充、最终状态），而那个 API 是**整条
    覆盖**的 —— 这条用例同时是「携带标记漏了一处」的回归测试。
    """
    gh = _Gh()
    await _run(gh, "/aifix 只有购物车为空时才崩", tmp_path)
    assert len(gh.statuses) >= 2, "至少写过「收到补充」和最终状态两条"

    _, second = await _run(gh, "/aifix", tmp_path)
    assert "只有购物车为空时才崩" in second["body"], (
        "光 /aifix 应当重跑上一次那份补充，而不是退回去读 issue 正文")


async def test_a_new_supplement_replaces_the_remembered_one(tmp_path):
    """再补一句就换成新的 —— 记住的是「上一次那份」，不是历史全集。"""
    gh = _Gh()
    await _run(gh, "/aifix 第一次的说法", tmp_path)
    _, second = await _run(gh, "/aifix 改口了，其实是并发下才崩", tmp_path)
    assert "并发下才崩" in second["body"]
    assert "第一次的说法" not in second["body"]


async def test_the_command_line_is_stripped_before_the_model_sees_it(tmp_path):
    """评论重跑时喂进去的正文**不以 `/aifix` 开头**。

    从前 `_from_comment` 原样取 issue 正文（`_from_issue` 会剥掉命令行），于是
    同一个 issue 的首次触发与评论重跑，喂给模型的东西不一样 —— 重跑那次的第
    一句是一个模型不认识的命令词。
    """
    gh = _Gh()
    _, seen = await _run(gh, "/aifix", tmp_path)
    assert not seen["body"].lstrip().startswith("/aifix"), seen["body"][:120]


async def test_the_supplement_path_still_checks_both_people(tmp_path):
    """两道权限判定在补充这条路上一个都不能少。

    issue 正文仍然进模型，所以「只限制触发者挡不住提示注入」那条理由原样成立：
    外人提一个藏了指令的 issue，等有权限的人顺手打上 `/aifix 补充两句`，
    模型读到的仍然是那段不可信的正文。
    """
    p = copy.deepcopy(_payload("/aifix 补充两句"))
    p["issue"]["user"]["login"] = "someone-else"
    p["issue"]["author_association"] = "CONTRIBUTOR"
    d = authorize(p)
    assert not d.allowed and d.notify
    assert "someone-else" in d.reason


# ------------------------------------------------------------ 回答

async def test_a_number_still_picks_the_option(tmp_path):
    """纯数字仍然走选项路径 —— 新语法没把编号挤掉。

    留着它是为了那条无歧义的审计记录：人选的就是模型自己列的第 N 项。
    """
    gh = _Gh()
    _, seen = await _run(gh, "/aifix 2", tmp_path, status_body=_ask_marker())
    assert "抛 ValueError" in seen["answer"]
    assert "收到答复" in "\n".join(b for _, b in gh.statuses)


async def test_free_text_answers_the_question_verbatim(tmp_path):
    """有待答问题时，非数字的文字是**用自己的话回答**，原文进提示词。

    这条路从前是禁的，理由写作「开放式回复要再过一次模型去解析意图」——
    而 `format_answer` 只是拼字符串，没有第二次模型调用。理由不成立，禁令已解。
    """
    gh = _Gh()
    _, seen = await _run(gh, "/aifix 空的时候该抛异常，别返回 None",
                         tmp_path, status_body=_ask_marker())
    assert "空的时候该抛异常，别返回 None" in seen["answer"]
    assert _ASK["question"] in seen["answer"], "答复要带着问题一起给，否则没有上下文"


async def test_a_free_text_answer_is_not_mistaken_for_a_supplement(tmp_path):
    """有问题在等时，文字**不该**被当成补充说明 —— 那会把答复喂成缺陷描述。"""
    gh = _Gh()
    _, seen = await _run(gh, "/aifix 该抛异常", tmp_path,
                         status_body=_ask_marker())
    assert seen.get("answer"), "应当走答复这条路"
    assert "该抛异常" not in seen.get("body", ""), "不该同时又当成缺陷补充"


async def test_a_bare_command_gives_up_the_pending_question_out_loud(tmp_path):
    """挂着问题却光打一个 `/aifix` = 「上一步出问题了，重试」。

    放弃那个提问是对的（不带答复重跑），但**必须明说**：默默丢掉一个人正准备
    回答的问题，是这条路上最容易让人以为「我答过了」的失败方式。
    """
    gh = _Gh()
    _, seen = await _run(gh, "/aifix", tmp_path, status_body=_ask_marker())
    assert not seen.get("answer"), "没带答复，就不该有答复"
    assert "放弃" in "\n".join(b for _, b in gh.statuses)


async def test_a_number_with_nothing_pending_says_so_instead_of_running(tmp_path):
    """没有待答问题却打了个纯数字 —— 出声，别当补充说明跑掉。

    一个光秃秃的整数不是缺陷描述，而跑一轮要花掉整份预算和几十分钟。这个人
    几乎肯定是在回答一个已经不在了的问题。
    """
    gh = _Gh()
    res, seen = await _run(gh, "/aifix 3", tmp_path, status_body="没有标记")
    assert res.path == "no_pending"
    assert not seen, "不该走到复现那一步"
    assert "没有待回答的问题" in "\n".join(b for _, b in gh.comments)


@pytest.mark.parametrize("choice", ["0", "9"])
async def test_an_out_of_range_number_is_still_refused(tmp_path, choice):
    """越界当场拒。放过去的话它会静静地按另一个选项去改代码。"""
    gh = _Gh()
    res, seen = await _run(gh, f"/aifix {choice}", tmp_path,
                           status_body=_ask_marker())
    assert res.path == "bad_choice"
    assert not seen


# ------------------------------------------------------------ 标记

def test_the_two_markers_coexist_in_one_comment():
    """两种标记住在同一条状态评论里，互不干扰。

    状态评论只有一条（不刷屏），所以待答问题和上一次那份补充必须并存。按 tag
    分开取，取错一种的后果是「答复被当成缺陷描述」这类静默错解。
    """
    body = "\n\n".join([
        "## 需要你回答一个问题",
        pending_store.encode_marker({**_ASK, "repro": {}}),
        pending_store.encode_last("并发下才崩"),
    ])
    assert pending_store.decode_marker(body)["question"] == _ASK["question"]
    assert pending_store.decode_last(body) == "并发下才崩"


def test_a_supplement_containing_a_comment_terminator_survives():
    """补充说明是**人写的自由文本**，里面完全可能出现 `-->`。

    裸 JSON 会被它当场截断 —— 后半截直接显示在 issue 上，而标记再也解析不
    出来。这和待答问题走 base64 是同一条理由，也是两者共用一份编解码的理由。
    """
    nasty = "改完之后 `x --> y` 这个箭头就没了 --> 就是这里"
    assert pending_store.decode_last(pending_store.encode_last(nasty)) == nasty


def test_no_marker_means_no_supplement_not_an_error():
    """首次触发就是这个状态：没有标记 = 没有上一次，不是出错。"""
    assert pending_store.decode_last("") == ""
    assert pending_store.decode_last("普通的一条评论") == ""
    assert pending_store.decode_last("<!-- aifix:last 这不是base64 -->") == ""


def test_the_ask_marker_is_not_read_as_a_supplement():
    """只有 ask 标记时，`decode_last` 必须给空 —— 拿问题当补充说明去跑，
    模型会认真地为「空购物车应该返回什么？」写一条复现测试。"""
    only_ask = pending_store.encode_marker({**_ASK, "repro": {}})
    assert pending_store.decode_last(only_ask) == ""


async def test_the_supplement_survives_an_unfixed_run(tmp_path):
    """「没修好」那条路也必须携带补充说明的标记。

    它是 0.3.1 新开的第四个状态评论写入点（推分支 + 回帖、不开 PR），而
    `_write_status` 的 docstring 早就写明了：让各个调用点自己记得带标记的话，
    漏掉任何一个都会让补充说明静默消失。

    漏掉的后果精确地是：人补充过一句、这一轮没修好，下一次光打 `/aifix`
    重跑读回的是 issue 正文 —— 表面照常工作，实际重跑的是另一件事，而两次
    的报告看起来一模一样。**没修好恰恰是最可能被重跑的那条路。**
    """
    unfixed = _state(report_md="# 报告\n\n修复 0/1",
                     results=[{"test_id": "tests/test_issue_1.py::test_x",
                               "verdict": "same", "attempts": 3,
                               "abort_reason": "max_attempts"}])
    gh = _Gh()
    seen: dict = {}

    async def _reproduce_fn(repo, adapter, config, title, issue_body):
        seen["body"] = issue_body
        return ReproduceOutcome(_repro())

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        return unfixed

    (tmp_path / "tests").mkdir(exist_ok=True)
    res = await handle(_payload("/aifix 只有购物车为空时才崩"), tmp_path,
                       AifixConfig(), gh, reproduce_fn=_reproduce_fn,
                       red_check_fn=_red, run_fn=_run_fn,
                       publish=lambda *a, **k: True, git=lambda *a, **k: "")
    assert res.path == "unfixed" and not gh.prs

    # 下一次光 /aifix，那句补充必须还在
    _, second = await _run(gh, "/aifix", tmp_path)
    assert "只有购物车为空时才崩" in second["body"], second["body"][:200]
