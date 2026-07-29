"""issue 事件的解析与授权判定。全部零 LLM。

载荷夹具来自 `gh api` 拉的**真实** issue / comment 对象（见 fixtures 里的
_note）。手写的 JSON 只能证明我们理解得自洽，证明不了 GitHub 真的这么发
（全局约束 4）。
"""
import copy
import json
from pathlib import Path

import pytest

from aifix.issue.event import authorize, load_payload

_FIXTURE = Path(__file__).parent / "fixtures" / "issue_comment_created.json"
_RAW = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _payload():
    """一份**会被放行**的载荷；每个否定用例用 _set 只偏离一个字段。

    夹具里的真实 author_association 是 CONTRIBUTOR、作者也不是仓库主 ——
    先把它调成放行态，否则每个否定用例都分不清是被哪一条挡下的。
    """
    p = copy.deepcopy(_RAW["payload"])
    owner = p["repository"]["owner"]["login"]
    p["issue"]["user"]["login"] = owner
    p["comment"]["author_association"] = "OWNER"
    p["comment"]["body"] = "/aifix"
    return p


def _set(p, dotted, value):
    cur = p
    keys = dotted.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value
    return p


# ---------------------------------------------------------------- 放行

def test_owner_command_on_own_issue_is_allowed():
    d = authorize(_payload())
    assert d.allowed is True, d.reason
    assert d.event.number == _RAW["payload"]["issue"]["number"]
    assert d.event.title == _RAW["payload"]["issue"]["title"]


def test_allowed_event_carries_the_repo_owner():
    d = authorize(_payload())
    assert d.event.owner == _RAW["payload"]["repository"]["owner"]["login"]


# ---------------------------------------------------------------- 命令面

def test_command_must_be_on_the_first_line():
    """全文搜索会把引用别人的话、或正文里贴的一段命令当成指令。"""
    p = _set(_payload(), "comment.body", "他说：\n/aifix\n我觉得先别跑")
    assert authorize(p).allowed is False


def test_carriage_returns_do_not_break_the_command():
    """GitHub 的评论正文用 CRLF。按 \\n 切完不 strip 的话，第一行是
    '/aifix\\r'，与 '/aifix' 不相等 —— 命令永远匹配不上，而且一声不吭。
    """
    p = _set(_payload(), "comment.body", "/aifix\r\n再跑一次看看\r\n")
    assert authorize(p).allowed is True


def test_a_plain_comment_is_ignored_silently():
    """绝大多数评论都不是命令。这一类不能回帖 —— 每条闲聊都被机器人回一句
    「这不是命令」，比不回还糟。
    """
    d = authorize(_set(_payload(), "comment.body", "我也遇到了"))
    assert d.allowed is False and d.notify is False


# ---------------------------------------------------------------- 事件面

def test_edited_comments_are_ignored():
    """只认 created：否则一条三个月前的旧评论被编辑成 /aifix 就能触发。"""
    p = _payload()
    p["action"] = "edited"
    assert authorize(p).allowed is False


def test_pull_request_comments_are_ignored():
    """issue_comment 对 PR 也会触发 —— GitHub 眼里 PR 就是一种 issue。

    判据是 issue 对象上有没有 pull_request 键；夹具里那份真 PR
    （cli/cli#1）证明这个键确实存在于真实载荷上，不是我们推想出来的。
    """
    p = _payload()
    p["issue"]["pull_request"] = _RAW["pull_request_issue"]["pull_request"]
    d = authorize(p)
    assert d.allowed is False and d.notify is False


def test_bot_comments_are_ignored():
    """自己回的帖不能把自己再唤醒一次。

    Actions 里用 GITHUB_TOKEN 发的评论不会再触发 workflow（GitHub 内建的
    防递归），但 repository_dispatch 那条路**永远会触发**，不受那层保护。
    """
    p = _set(_payload(), "comment.user.type", "Bot")
    d = authorize(p)
    assert d.allowed is False and d.notify is False


# ---------------------------------------------------------------- 授权面

def test_non_owner_commenter_is_refused_and_told_why():
    """看起来是命令但没权限 —— 必须回帖。静默丢弃会让人以为它在跑了，
    而这正是本项目栽过十次以上的那种失败：不报错，只有承诺是假的。
    """
    for assoc in ("CONTRIBUTOR", "COLLABORATOR", "MEMBER", "NONE"):
        d = authorize(_set(_payload(), "comment.author_association", assoc))
        assert d.allowed is False, assoc
        assert d.notify is True, assoc
        assert d.reason, assoc


def test_contributor_is_not_a_trust_signal():
    """CONTRIBUTOR 只意味着「有 commit 进过这个仓库」—— 一年前合过一个改
    错别字的 PR，就永久是 CONTRIBUTOR。上一条已经覆盖，这里单列是因为把
    它误当可信身份是最常见的写法。"""
    d = authorize(_set(_payload(), "comment.author_association", "CONTRIBUTOR"))
    assert d.allowed is False


def test_outsider_issue_is_refused_even_when_owner_comments():
    """只限制触发者挡不住注入。

    攻击路径是：外人提一个藏了指令的 issue，等仓库主觉得该修、顺手打上
    /aifix —— 而仓库主本来就想修 bug，那一步门槛低得可怜。模型读到的每个字
    必须都是仓库主自己写的，注入面才真的归零。
    """
    d = authorize(_set(_payload(), "issue.user.login", "someone-else"))
    assert d.allowed is False and d.notify is True
    assert "issue" in d.reason


# ---------------------------------------------------------------- 载入

def test_load_payload_reads_the_event_file(tmp_path):
    f = tmp_path / "event.json"
    f.write_text(json.dumps(_payload()), encoding="utf-8")
    assert load_payload(f)["action"] == "created"


def test_load_payload_raises_a_readable_error_when_missing(tmp_path):
    with pytest.raises(RuntimeError) as e:
        load_payload(tmp_path / "nope.json")
    assert "GITHUB_EVENT_PATH" in str(e.value)
