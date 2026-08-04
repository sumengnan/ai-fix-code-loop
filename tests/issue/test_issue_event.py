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
    """一份**会被放行**的评论触发载荷；每个否定用例用 _set 只偏离一个字段。

    夹具里的真实 author_association 是 CONTRIBUTOR、作者也不是仓库主 ——
    先把它调成放行态，否则每个否定用例都分不清是被哪一条挡下的。

    **两个 author_association 都要设**，它们答的是不同的问题：
      comment.author_association —— 谁按的按钮
      issue.author_association   —— 谁写的那段要喂给模型的文本
    评论触发这条路上模型读的是 issue 正文，所以两边都必须可信。
    """
    p = copy.deepcopy(_RAW["payload"])
    owner = p["repository"]["owner"]["login"]
    p["issue"]["user"]["login"] = owner
    p["issue"]["author_association"] = "OWNER"
    p["comment"]["author_association"] = "OWNER"
    p["comment"]["body"] = "/aifix"
    return p


def _issue_payload(body="/aifix\n购物车为空时 total() 抛 KeyError"):
    """issue 正文触发的载荷。

    夹具是一份真实的 `issue_comment` 事件，这里去掉 comment 键、把 action 换成
    opened 来当 `issues` 事件用 —— issue 与 repository 两个对象仍是 gh api 拉
    下来的真货，而这条路径读的正是它们。
    """
    p = copy.deepcopy(_RAW["payload"])
    p.pop("comment", None)
    p["action"] = "opened"
    p["issue"]["author_association"] = "OWNER"
    p["issue"]["body"] = body
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

def test_untrusted_commenter_is_refused_and_told_why():
    """看起来是命令但没权限 —— 必须回帖。静默丢弃会让人以为它在跑了，
    而这正是本项目栽过十次以上的那种失败：不报错，只有承诺是假的。
    """
    for assoc in ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
        d = authorize(_set(_payload(), "comment.author_association", assoc))
        assert d.allowed is False, assoc
        assert d.notify is True, assoc
        assert d.reason, assoc


def test_contributor_is_not_a_trust_signal():
    """CONTRIBUTOR 只意味着「有 commit 进过这个仓库」—— 一年前合过一个改
    错别字的 PR，就永久是 CONTRIBUTOR。它**没有 write 权限**，所以不该能
    驱动一个会改代码开 PR 的东西。"""
    d = authorize(_set(_payload(), "comment.author_association", "CONTRIBUTOR"))
    assert d.allowed is False


def test_collaborator_is_trusted():
    """COLLABORATOR 按定义就有 write 权限 —— 他本来就能直接推代码，
    让他驱动 aifix 不增加任何新风险。这是「触发权 = 已经能改这个仓库的人」
    这条原则的落点。
    """
    p = _payload()
    _set(p, "comment.author_association", "COLLABORATOR")
    _set(p, "issue.author_association", "COLLABORATOR")
    assert authorize(p).allowed is True


def test_member_counts_only_on_organization_repos():
    """MEMBER 是「组织成员」。个人仓库上不该出现这个值，真出现了也不放行 ——
    守卫宁可多拦不可漏放。
    """
    org = _payload()
    _set(org, "repository.owner.type", "Organization")
    _set(org, "comment.author_association", "MEMBER")
    _set(org, "issue.author_association", "MEMBER")
    assert authorize(org).allowed is True

    personal = _payload()
    _set(personal, "repository.owner.type", "User")
    _set(personal, "comment.author_association", "MEMBER")
    _set(personal, "issue.author_association", "MEMBER")
    assert authorize(personal).allowed is False


def test_untrusted_issue_author_is_refused_even_when_a_trusted_user_comments():
    """**只限制触发者挡不住注入。**

    评论触发这条路上，模型读的是 issue 正文 —— 那段文本的作者是提 issue 的
    人，不是按按钮的人。攻击路径是：外人提一个藏了指令的 issue，等有权限的
    人觉得该修、顺手打上 /aifix。

    所以这条路要两边都可信：按按钮的人**和**写正文的人。
    """
    p = _payload()
    # 两个字段一起改才是一份**真实**的外人 issue：只改 author_association、
    # 而作者仍是仓库账号本身，GitHub 不会发出这种载荷。
    _set(p, "issue.author_association", "NONE")
    _set(p, "issue.user.login", "someone-else")
    d = authorize(p)
    assert d.allowed is False and d.notify is True
    assert "issue" in d.reason


def test_the_repo_owners_own_issue_is_trusted_without_an_association():
    """登录名等于仓库账号本身，是比 author_association 更确定的一条信号。

    留着它是防御性的：万一某种事件形状里 author_association 缺失或换了取值，
    仓库主不至于被自己的工具锁在门外。
    """
    p = _payload()
    _set(p, "issue.author_association", "")
    assert authorize(p).allowed is True


# ---------------------------------------------------------- 白名单（乙档）

def test_allowlist_admits_someone_with_no_association():
    """按 author_association 认不出来的人，可以用显式白名单放进来。"""
    p = _payload()
    _set(p, "comment.author_association", "NONE")
    _set(p, "issue.author_association", "NONE")
    login = p["comment"]["user"]["login"]
    _set(p, "issue.user.login", login)
    assert authorize(p).allowed is False, "没有白名单时应当被拒"
    assert authorize(p, allowed_users=frozenset({login.casefold()})).allowed is True


def test_allowlist_is_case_insensitive():
    """GitHub 的登录名大小写不敏感 —— Alice 与 alice 是同一个人。
    区分大小写的话，名单里写错一个字母就是静默失效。
    """
    p = _payload()
    _set(p, "comment.author_association", "NONE")
    _set(p, "issue.author_association", "NONE")
    _set(p, "comment.user.login", "Alice")
    _set(p, "issue.user.login", "Alice")
    assert authorize(p, allowed_users=frozenset({"alice"})).allowed is True


def test_allowlist_matches_whole_logins_not_substrings():
    """`alice` 不能放行 `alicexyz`。裸子串匹配会把白名单变成前缀通行证。"""
    p = _payload()
    _set(p, "comment.author_association", "NONE")
    _set(p, "issue.author_association", "NONE")
    _set(p, "comment.user.login", "alicexyz")
    _set(p, "issue.user.login", "alicexyz")
    assert authorize(p, allowed_users=frozenset({"alice"})).allowed is False


def test_allowlist_still_requires_both_sides_on_the_comment_path():
    """白名单放行的是「这个人」，不是「这次触发」——评论触发仍然两边都要查。"""
    p = _payload()
    _set(p, "comment.author_association", "NONE")
    _set(p, "comment.user.login", "alice")
    _set(p, "issue.author_association", "NONE")
    _set(p, "issue.user.login", "outsider")
    d = authorize(p, allowed_users=frozenset({"alice"}))
    assert d.allowed is False and d.notify is True


# ------------------------------------------------- issue 正文触发（新入口）

def test_issue_body_starting_with_the_command_triggers():
    """开 issue 就能触发，不用再补一条评论。

    这条路上**触发的动作与被读的文本是同一个人写的同一个对象** —— 一条判据
    同时管住「谁按的按钮」和「谁写的文本」，注入面是结构上归零的。
    """
    d = authorize(_issue_payload())
    assert d.allowed is True, d.reason
    assert d.event.choice is None


def test_the_command_line_is_stripped_from_the_body_given_to_the_model():
    """`/aifix` 是给机器看的标记，不是缺陷描述的一部分。
    原样喂进去的话，模型的第一句上下文是一个它不认识的命令词。
    """
    d = authorize(_issue_payload("/aifix\n购物车为空时 total() 抛 KeyError"))
    assert d.event.body == "购物车为空时 total() 抛 KeyError"


def test_an_issue_without_the_command_is_ignored_silently():
    """绝大多数 issue 都不是命令。这一类一个字都不能回。"""
    d = authorize(_issue_payload("购物车为空时崩溃了，帮我看看"))
    assert d.allowed is False and d.notify is False


def test_an_untrusted_issue_author_is_refused_and_told_why():
    """你要的那条：不是有权限的人但以 /aifix 开头 → 明确告诉他不能触发。"""
    p = _issue_payload()
    _set(p, "issue.author_association", "NONE")
    d = authorize(p)
    assert d.allowed is False and d.notify is True
    assert d.reason


def test_edited_issue_bodies_do_not_trigger():
    """只认 opened。改自己的 issue 正文是**完全静默**的 —— 一条半年前的
    issue 被编辑成 /aifix 开头就能触发，而没有任何人会收到通知。
    """
    p = _issue_payload()
    p["action"] = "edited"
    assert authorize(p).allowed is False


def test_bot_issues_do_not_trigger():
    p = _set(_issue_payload(), "issue.user.type", "Bot")
    d = authorize(p)
    assert d.allowed is False and d.notify is False


def test_pull_requests_are_ignored_on_the_issue_path_too():
    p = _issue_payload()
    p["issue"]["pull_request"] = _RAW["pull_request_issue"]["pull_request"]
    d = authorize(p)
    assert d.allowed is False and d.notify is False


# ---------------------------------------------------------------- 载入

def test_load_payload_reads_the_event_file(tmp_path):
    f = tmp_path / "event.json"
    f.write_text(json.dumps(_payload()), encoding="utf-8")
    assert load_payload(f)["action"] == "created"


def test_load_payload_raises_a_readable_error_when_missing(tmp_path):
    with pytest.raises(RuntimeError) as e:
        load_payload(tmp_path / "nope.json")
    assert "GITHUB_EVENT_PATH" in str(e.value)


def test_the_command_now_takes_free_text_instead_of_refusing_it():
    """`/aifix <文字>` 从「命令认不出来」变成合法写法。

    实测踩过（2026-08-04）：`/aifix 重跑一下把 PR 开出来` 让 workflow 的 `if:`
    （用的是 startsWith）**起了 job**、装完全套依赖，然后被静默丢弃 —— 没有
    评论、没有 reaction。那次的修法是让它出声拒绝；这次是让它真的工作。

    词法层只把文字原样带出来，**不判断它是回答还是补充说明** —— 那要看 issue
    上有没有待答问题，而 authorize 是纯函数，不碰网络（分类见 handle）。
    """
    payload = {
        "action": "created",
        "issue": {"number": 7, "user": {"login": "o"},
                  "author_association": "OWNER", "body": "/aifix\n正文"},
        "repository": {"owner": {"login": "o", "type": "User"}},
        "comment": {"body": "/aifix 重跑一下把 PR 开出来", "id": 9,
                    "user": {"login": "o", "type": "User"},
                    "author_association": "OWNER"},
    }
    d = authorize(payload)
    assert d.allowed is True, d.reason
    assert d.event.text == "重跑一下把 PR 开出来"
    assert d.event.choice is None


def test_an_ordinary_comment_still_stays_silent():
    """反向：不以 /aifix 开头的评论一个字都不能回 —— 否则每条闲聊都被机器人
    怼一句「这不是命令」，比不回还糟。"""
    payload = {
        "action": "created",
        "issue": {"number": 7, "user": {"login": "o"},
                  "author_association": "OWNER", "body": "/aifix\n正文"},
        "repository": {"owner": {"login": "o", "type": "User"}},
        "comment": {"body": "我觉得这个 bug 挺有意思", "id": 9,
                    "user": {"login": "o", "type": "User"},
                    "author_association": "OWNER"},
    }
    d = authorize(payload)
    assert d.allowed is False
    assert d.notify is False
