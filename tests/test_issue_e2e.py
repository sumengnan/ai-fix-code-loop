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
    # 仓库没被碰过：没有多出提交，也没有留下文件
    assert _git(buggy_repo, "status", "--porcelain").strip() == ""
