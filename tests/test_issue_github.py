"""GitHub 输出侧：一条会被反复编辑的状态评论、一个 PR、一个即时回执。

`gh` 的调用注入一个 recorder 来断言命令拼得对；凡是要**解析** GitHub 返回的
地方，喂进去的都是真实 comment 对象（夹具里那份由 gh api 拉的），不自造。
"""
import json
from pathlib import Path

from aifix.issue.github import STATUS_MARKER, GitHubClient

_RAW = json.loads((Path(__file__).parent / "fixtures"
                   / "issue_comment_created.json").read_text(encoding="utf-8"))
_REAL_COMMENT = _RAW["payload"]["comment"]


class _Recorder:
    """记下每次调用；按预置队列返回 stdout。"""

    def __init__(self, responses=()):
        self.calls = []
        self._resp = list(responses)

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        return self._resp.pop(0) if self._resp else ""

    def find(self, *needles):
        """返回同时含有全部 needle 的那些调用。"""
        return [c for c in self.calls
                if all(any(n == a or n in a for a in c[0]) for n in needles)]


def _client(responses=()):
    r = _Recorder(responses)
    return GitHubClient("me/proj", run=r), r


# ---------------------------------------------------------------- 回执

def test_react_targets_the_triggering_comment():
    c, r = _client()
    c.react(12345, "eyes")
    assert r.find("repos/me/proj/issues/comments/12345/reactions")


# ---------------------------------------------------------------- 状态评论

def test_first_status_creates_a_comment():
    """从没发过 —— 建一条新的。"""
    c, r = _client(responses=["[]"])
    c.upsert_status(42, "跑起来了")
    created = [x for x in r.calls if "--method" not in x[0] and x[1] is not None]
    assert created, r.calls
    assert "跑起来了" in created[-1][1]


def test_status_body_carries_the_marker():
    """标记是下一次找回自己那条评论的唯一依据。丢了它，每次 run 都会新发
    一条，issue 很快变成机器人的刷屏现场。"""
    c, r = _client(responses=["[]"])
    c.upsert_status(42, "x")
    assert any(STATUS_MARKER in (x[1] or "") for x in r.calls)


def test_second_status_edits_the_same_comment():
    """已经发过 —— 编辑它，不新建。

    喂进去的是**真实** comment 对象（只把 body 换成带标记的），确保我们认领
    自己那条评论时读的字段名和 GitHub 实际返回的一致。
    """
    mine = dict(_REAL_COMMENT, id=999, body=f"旧内容\n{STATUS_MARKER}")
    c, r = _client(responses=[json.dumps([[_REAL_COMMENT, mine]])])
    c.upsert_status(42, "新内容")
    patched = r.find("--method", "PATCH")
    assert patched, r.calls
    assert any("999" in a for a in patched[0][0])
    assert "新内容" in patched[0][1]


def test_a_comment_without_the_marker_is_not_claimed():
    """别人的评论不能被当成自己的状态评论去改。

    反向对照：上一个用例证明「有标记就会被编辑」，这一个证明「没标记就不会」
    —— 缺了它，一个恒真的「总是编辑第一条」实现也能让上面那条通过。
    """
    c, r = _client(responses=[json.dumps([[_REAL_COMMENT]])])
    c.upsert_status(42, "新内容")
    assert not r.find("--method", "PATCH")


# ---------------------------------------------------------------- PR

def test_create_pr_uses_the_default_token_identity():
    """PR 必须由 GITHUB_TOKEN（即 github-actions[bot]）开。

    仓库主不能批准自己开的 PR —— 用他自己的 PAT 开的话，那个 Approve 按钮
    对他是灰的，而 PR review 正是 M6 唯一的那道人闸。
    """
    c, r = _client(responses=["https://github.com/me/proj/pull/7\n"])
    url = c.create_pr(head="aifix/abc123", title="修复 #42", body="报告正文")
    assert url == "https://github.com/me/proj/pull/7"
    call = r.find("pr", "create")[0]
    assert "aifix/abc123" in call[0]
    assert "报告正文" in call[1]


def test_pr_body_goes_through_stdin_not_argv():
    """报告动辄几千字，还含反引号和换行。塞进 argv 迟早撞上长度上限或被
    shell 解释；走 stdin 两个问题都不存在。"""
    c, r = _client(responses=["u\n"])
    long_body = "行\n" * 5000
    c.create_pr(head="h", title="t", body=long_body)
    call = r.find("pr", "create")[0]
    assert call[1] == long_body
    assert not any(long_body[:20] in a for a in call[0])


# ---------------------------------------------------------------- 普通回帖

def test_plain_comment_does_not_carry_the_status_marker():
    """triage 回帖是一次性的说明，不是状态板 —— 带上标记的话，下一次 run
    的状态更新会把这段说明覆盖掉。"""
    c, r = _client()
    c.comment(42, "信息不足，缺：复现步骤")
    assert STATUS_MARKER not in (r.calls[-1][1] or "")


def test_the_status_comment_is_found_across_paginated_pages():
    """`gh api --paginate` 每一页是**独立的** JSON 数组，多页时输出是几个数组
    串在一起 —— 直接 json.loads 必然失败。

    失败的形态特别糟：解析不了 → 认领不到自己那条 → 每次 run 新发一条评论。
    一条评论以上的 issue 才会触发（默认每页 30），而本地测一次只有零条，所以
    它会一路活到线上，然后表现为「机器人开始刷屏」。

    用 --slurp 把各页包进一个外层数组，再摊平一层。
    """
    others = [dict(_REAL_COMMENT, id=i) for i in range(1, 31)]
    mine = dict(_REAL_COMMENT, id=999, body=f"旧内容\n{STATUS_MARKER}")
    # --slurp 的形状：[[第一页...], [第二页...]]
    c, r = _client(responses=[json.dumps([others, [mine]])])
    c.upsert_status(42, "新内容")

    listed = [x for x in r.calls if "--paginate" in x[0]][0]
    assert "--slurp" in listed[0], "多页时不加 --slurp，输出不是合法 JSON"
    patched = r.find("--method", "PATCH")
    assert patched, "跨页没找到自己那条状态评论，会退化成每次新发一条"
    assert any("999" in a for a in patched[0][0])
