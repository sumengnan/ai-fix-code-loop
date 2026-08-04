"""M6 端到端：一条评论进去，一个 PR 出来。

**只有模型和 GitHub 是替身**，其余全是真的：真的 reproducer 落盘、真的红检
（跑 pytest）、真的 commit 进 HEAD、真的 run_once（worktree、baseline、
detect、fix、verify、交付分支）、真的 push 到一个 bare 远端。

这是能在没有 runner 与 API key 的情况下走到的最深处。它盖不住的只剩两件事：
Actions 的 YAML 语义（由 test_workflow.py 钉住），以及 `gh` 命令真的被 GitHub
接受（那需要真账号）。
"""
import json
import subprocess
from pathlib import Path

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.config import AifixConfig
from aifix.issue.handle import handle

_RAW = json.loads((Path(__file__).parent / "fixtures"
                   / "issue_comment_created.json").read_text(encoding="utf-8"))

# 复现测试：在 buggy_repo 上真的会红（add 少算了，返回 -1 而不是 5）
_REPRO_JSON = json.dumps({
    "can_reproduce": True,
    "test_file": "tests/test_issue_42.py",
    "test_code": ("from calc import add\n\n\n"
                  "def test_add_two_and_three():\n    assert add(2, 3) == 5\n"),
    "target_test_id": "tests/test_issue_42.py::test_add_two_and_three",
    "missing_info": [],
}, ensure_ascii=False)

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
}, ensure_ascii=False)

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""


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


class _Gh:
    def __init__(self):
        self.comments, self.statuses, self.prs, self.reactions = [], [], [], []

    def react(self, comment_id, emoji="eyes"):
        self.reactions.append(comment_id)

    def comment(self, issue, body):
        self.comments.append(body)

    def upsert_status(self, issue, body):
        self.statuses.append(body)

    def status_body(self, issue):
        """后写的整条覆盖先写的 —— 与真客户端「只维护一条状态评论」一致。

        这条评论是待答问题与上一次那份补充说明的持久层，替身恒返回空串的话，
        「跨轮活下来」这件事在测试里根本发生不了。
        """
        return self.statuses[-1] if self.statuses else ""

    def create_pr(self, head, title, body, base=None):
        self.prs.append({"head": head, "title": title, "body": body})
        return "https://example.invalid/pr/1"


def _payload(title, body):
    p = json.loads(json.dumps(_RAW["payload"]))
    owner = p["repository"]["owner"]["login"]
    p["issue"]["user"]["login"] = owner
    p["issue"]["number"] = 42
    p["issue"]["title"] = title
    p["issue"]["body"] = body
    p["comment"]["author_association"] = "OWNER"
    p["comment"]["body"] = "/aifix"
    return p


def _issue_payload(title, body):
    """issue 正文触发的载荷 —— 没有 comment 键，action 是 opened。"""
    p = json.loads(json.dumps(_RAW["payload"]))
    p.pop("comment", None)
    p["action"] = "opened"
    owner = p["repository"]["owner"]["login"]
    p["issue"]["user"]["login"] = owner
    p["issue"]["author_association"] = "OWNER"
    p["issue"]["number"] = 42
    p["issue"]["title"] = title
    p["issue"]["body"] = body
    return p


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


async def test_a_comment_becomes_a_pr_with_the_repro_and_the_fix(
        buggy_repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(buggy_repo, "remote", "add", "origin", str(bare))
    _git(buggy_repo, "push", "-q", "origin", "main")

    gh = _Gh()
    # 复现用一轮文本；修复用「打补丁 → 说完了」两轮
    reproducer = _Scripted([_text(_REPRO_JSON)])
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("改好了")])

    async def _reproduce(repo, adapter, config, title, body, client=None):
        from aifix.reproduce import reproduce
        return await reproduce(repo, adapter, config, title, body,
                               client=reproducer)

    async def _run(repo, config, run_id, only_test=None, **k):
        from aifix.cli import run_once
        return await run_once(repo, config, run_id=run_id, only_test=only_test,
                              detector_client=_Scripted([_text(_DIAG)]),
                              fixer_client=fixer)

    res = await handle(
        _payload("add 算错了", "add(2, 3) 返回 -1，期望 5。"),
        buggy_repo, AifixConfig(budget_tokens=100_000), gh,
        reproduce_fn=_reproduce, run_fn=_run,
        publish=lambda repo, run_id, **k: False)

    assert res.exit_code == 0, res
    assert res.path == "delivered", res
    assert gh.reactions == [_RAW["payload"]["comment"]["id"]]

    # 交付分支真的推到了远端，且带着**两个** commit：复现测试 + 修复
    branch = gh.prs[0]["head"]
    log = _git(bare, "log", "--format=%s", f"{branch}", "-3")
    assert "test: 复现 #42" in log, log
    assert "fix: tests/test_issue_42.py::test_add_two_and_three" in log, log

    # 分支上的 calc.py 真的被改对了
    blob = _git(bare, "show", f"{branch}:calc.py")
    assert "a + b" in blob and "a - b" not in blob

    # 复现测试也在分支上 —— 报告说「已修复」时，分支上必须真的有东西
    tree = _git(bare, "ls-tree", "-r", "--name-only", branch)
    assert "tests/test_issue_42.py" in tree

    assert "未修复" not in gh.prs[0]["title"]
    assert gh.statuses and "修好了" in gh.statuses[-1]


async def test_an_unreproducible_issue_stops_before_spending_on_the_fixer(
        buggy_repo, tmp_path):
    """写不出复现就不该启动 fixer —— 那是整条链路里最贵的一步。"""
    gh = _Gh()
    ran = []

    async def _reproduce(repo, adapter, config, title, body, client=None):
        from aifix.reproduce import reproduce
        return await reproduce(
            repo, adapter, config, title, body,
            client=_Scripted([_text(json.dumps(
                {"can_reproduce": False,
                 "missing_info": ["没说触发的输入", "没说期望输出"]}))]))

    async def _run(*a, **k):
        ran.append(1)
        raise AssertionError("不该走到核心循环")

    res = await handle(_payload("有问题", "反正就是不对"), buggy_repo,
                       AifixConfig(), gh, reproduce_fn=_reproduce, run_fn=_run)
    assert res.path == "no_repro" and not ran
    assert not gh.prs
    assert "没说触发的输入" in gh.comments[-1]
    # 源码没被碰过：没有多出提交、没有留下测试文件。
    #
    # 但 `.aifix/` **应该**在 —— 这条通路现在会落一份 trace（模型读了什么、
    # 为什么放弃）。第一次真跑时它没有，于是失败的那一轮什么证据都没留下。
    dirty = [ln for ln in _git(buggy_repo, "status", "--porcelain").splitlines()
             if ln.strip()]
    assert dirty == ["?? .aifix/"], dirty
    assert (buggy_repo / ".aifix").is_dir()
    assert not list((buggy_repo / "tests").glob("test_issue_*.py"))


async def test_an_issue_body_becomes_a_pr_without_any_comment(
        buggy_repo, tmp_path):
    """**主入口的端到端**：开一个 /aifix 开头的 issue，不评论任何东西。

    单测钉住了 authorize() 认这条路，但认得出不等于走得通 —— handle() 里有一处
    `gh.react(ev.comment_id)`，而这条路上根本没有那条评论。钉住三件事：
      1. 整条流水线走到底，PR 真的开出来了
      2. **不往 0 号评论加 reaction** —— 那会打到别人的帖子上
      3. 交给模型的正文里**没有那行 `/aifix`**
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(buggy_repo, "remote", "add", "origin", str(bare))
    _git(buggy_repo, "push", "-q", "origin", "main")

    gh = _Gh()
    seen_body = []
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("改好了")])

    async def _reproduce(repo, adapter, config, title, body, client=None):
        seen_body.append(body)
        from aifix.reproduce import reproduce
        return await reproduce(repo, adapter, config, title, body,
                               client=_Scripted([_text(_REPRO_JSON)]))

    async def _run(repo, config, run_id, only_test=None, **k):
        from aifix.cli import run_once
        return await run_once(repo, config, run_id=run_id, only_test=only_test,
                              detector_client=_Scripted([_text(_DIAG)]),
                              fixer_client=fixer)

    res = await handle(
        _issue_payload("add 算错了", "/aifix\nadd(2, 3) 返回 -1，期望 5。"),
        buggy_repo, AifixConfig(budget_tokens=100_000), gh,
        reproduce_fn=_reproduce, run_fn=_run,
        publish=lambda repo, run_id, **k: False)

    assert res.exit_code == 0 and res.path == "delivered", res
    # 没有评论可加回执 —— 加到 0 号上就是往别人的帖子上打表情
    assert gh.reactions == []
    # 命令那行不该进模型的上下文
    assert seen_body == ["add(2, 3) 返回 -1，期望 5。"], seen_body

    branch = gh.prs[0]["head"]
    blob = _git(bare, "show", f"{branch}:calc.py")
    assert "a + b" in blob and "a - b" not in blob


async def test_an_outsider_issue_is_refused_with_an_explanation(buggy_repo):
    """你要的那条：没权限的人打了 /aifix，**要收到一条说明**，而不是静默丢弃。

    并且一分钱都不能花 —— 拒绝发生在 reproduce 之前。
    """
    gh = _Gh()
    spent = []

    async def _reproduce(*a, **k):
        spent.append(1)
        raise AssertionError("拒绝之后不该再调用模型")

    p = _issue_payload("add 算错了", "/aifix\nadd(2, 3) 返回 -1。")
    p["issue"]["user"]["login"] = "someone-else"
    p["issue"]["author_association"] = "NONE"

    res = await handle(p, buggy_repo, AifixConfig(), gh,
                       reproduce_fn=_reproduce,
                       run_fn=lambda *a, **k: None)

    assert res.path == "refused" and res.exit_code == 0, res
    assert not spent, "拒绝路径上不该有任何模型调用"
    assert gh.comments, "必须回帖 —— 静默丢弃会让人以为它已经在跑了"
    assert "权限" in gh.comments[-1]
    assert not gh.prs


async def test_the_allowlist_lets_an_outsider_through(buggy_repo, tmp_path):
    """`AIFIX_ALLOWED_USERS` 里的人能触发，哪怕 author_association 是 NONE。

    这条钉的是**整条线接没接上** —— 配置读得到、handle 传得进去、authorize 用得上。
    断在任何一环，表现都是「名单一声不吭地不起作用」。
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(buggy_repo, "remote", "add", "origin", str(bare))
    _git(buggy_repo, "push", "-q", "origin", "main")

    gh = _Gh()
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("改好了")])

    async def _reproduce(repo, adapter, config, title, body, client=None):
        from aifix.reproduce import reproduce
        return await reproduce(repo, adapter, config, title, body,
                               client=_Scripted([_text(_REPRO_JSON)]))

    async def _run(repo, config, run_id, only_test=None, **k):
        from aifix.cli import run_once
        return await run_once(repo, config, run_id=run_id, only_test=only_test,
                              detector_client=_Scripted([_text(_DIAG)]),
                              fixer_client=fixer)

    p = _issue_payload("add 算错了", "/aifix\nadd(2, 3) 返回 -1，期望 5。")
    p["issue"]["user"]["login"] = "Alice"       # 大小写与名单里的不一致
    p["issue"]["author_association"] = "NONE"

    res = await handle(
        p, buggy_repo,
        AifixConfig(budget_tokens=100_000, allowed_users=["alice"]), gh,
        reproduce_fn=_reproduce, run_fn=_run,
        publish=lambda repo, run_id, **k: False)

    assert res.exit_code == 0 and res.path == "delivered", res
    assert gh.prs


async def test_an_unfixed_run_comments_instead_of_opening_a_pr(
        buggy_repo, tmp_path):
    """复现写出来了、修没修好 —— **不开 PR**，回帖说清楚。

    以前这条路照样开 PR（标题标「未修复」）。理由是「一条红着的复现测试本身
    就是产出」—— 那句仍然成立，所以**分支照推**，丢掉它等于扔掉这次 run 里
    唯一有价值的东西（它是真花了钱写出来的）。

    改的是 PR 那一步：一个永远合不进去的 PR 会堆在列表里，而 PR 的语义是
    「这些改动请你合」。没修好时该说的是「我走到哪儿、卡在哪儿、东西在哪个
    分支上」，那是一条评论的形状，不是一个 PR 的形状。
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(buggy_repo, "remote", "add", "origin", str(bare))
    _git(buggy_repo, "push", "-q", "origin", "main")

    gh = _Gh()
    reproducer = _Scripted([_text(_REPRO_JSON)])
    # 修复侧只说话不改文件 —— 撞空 diff 守卫，最后判「未改善」
    fixer = _Scripted([_text("我看了一下，感觉没什么要改的")])

    async def _reproduce(repo, adapter, config, title, body, client=None):
        from aifix.reproduce import reproduce
        return await reproduce(repo, adapter, config, title, body,
                               client=reproducer)

    async def _run(repo, config, run_id, only_test=None, **k):
        from aifix.cli import run_once
        return await run_once(repo, config, run_id=run_id, only_test=only_test,
                              detector_client=_Scripted([_text(_DIAG)]),
                              fixer_client=fixer)

    res = await handle(
        _payload("add 算错了", "add(2, 3) 返回 -1，期望 5。"),
        buggy_repo, AifixConfig(budget_tokens=100_000, max_attempts=1), gh,
        reproduce_fn=_reproduce, run_fn=_run,
        publish=lambda repo, run_id, **k: False)

    # 一个 PR 都没开
    assert gh.prs == [], gh.prs
    assert res.pr_url is None
    assert res.path == "unfixed", res
    assert res.exit_code == 0, res      # 没修好是正常收场，不是故障

    # 但分支推上去了，复现测试在里面 —— 那是这次唯一的产出
    branch = f"aifix/{res.run_id}"
    tree = _git(bare, "ls-tree", "-r", "--name-only", branch)
    assert "tests/test_issue_42.py" in tree, tree

    # 回帖必须说清楚三件事：没修好、东西在哪、怎么接手
    said = "\n".join(gh.statuses + gh.comments)
    assert "没能修好" in said, said
    assert branch in said, said
    assert "git fetch" in said, said        # 给出接手的命令
    # 报告照常带上（判定、尝试次数、成本都在里面）。
    # 分母不写死：buggy_repo 自带一个失败用例，加上这条复现测试是 2 个 ——
    # 而这条测试要钉的是「报告有没有跟着回帖出去」，不是夹具里有几个红。
    assert "修复：**0 /" in said, said
    assert "未改善" in said, said
