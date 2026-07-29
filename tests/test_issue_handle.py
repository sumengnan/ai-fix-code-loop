"""`aifix issue handle` 的编排：三条交付通路与退出码。

打桩 reproduce / red_check / run_once —— 这一层的职责是**选哪条路**，
底下那三步各自的正确性由它们自己的测试负责。
"""
import copy
import json
from pathlib import Path

import pytest

from aifix.agents.reproducer import Reproduction
from aifix.config import AifixConfig
from aifix.issue.handle import handle
from aifix.reproduce import ReproduceOutcome

_RAW = json.loads((Path(__file__).parent / "fixtures"
                   / "issue_comment_created.json").read_text(encoding="utf-8"))

_REPRO = Reproduction(
    can_reproduce=True, test_file="tests/test_issue_1.py",
    test_code="def test_x():\n    assert False\n",
    target_test_id="tests/test_issue_1.py::test_x")


def _payload(body="/aifix"):
    p = copy.deepcopy(_RAW["payload"])
    owner = p["repository"]["owner"]["login"]
    p["issue"]["user"]["login"] = owner
    p["comment"]["author_association"] = "OWNER"
    p["comment"]["body"] = body
    return p


class _Gh:
    def __init__(self):
        self.comments, self.statuses, self.prs, self.reactions = [], [], [], []

    def react(self, comment_id, emoji="eyes"):
        self.reactions.append((comment_id, emoji))

    def comment(self, issue, body):
        self.comments.append((issue, body))

    def upsert_status(self, issue, body):
        self.statuses.append((issue, body))

    def create_pr(self, head, title, body, base=None):
        self.prs.append({"head": head, "title": title, "body": body})
        return "https://example.invalid/pr/1"


def _state(**over):
    """run_once 返回的状态。默认：修好了一个，没有别的红。"""
    return {
        "report_md": "# 报告\n\n修复 1/1",
        "branch": "aifix/deadbeef",
        "baseline_ids": ["tests/test_issue_1.py::test_x"],
        "results": [{"test_id": "tests/test_issue_1.py::test_x",
                     "verdict": "better", "attempts": 1, "abort_reason": None}],
        "abort_kind": None,
    } | over


def _fakes(repro=None, red=(True, ""), state=None):
    """三个被打桩的依赖 + 一份 git 调用记录。"""
    calls = {"git": []}

    async def _reproduce(*a, **k):
        return repro if repro is not None else ReproduceOutcome(_REPRO, tokens=5)

    async def _red(*a, **k):
        return red

    async def _run(*a, **k):
        return _state() if state is None else state

    def _git(repo, *args):
        calls["git"].append(list(args))
        return ""

    return dict(reproduce_fn=_reproduce, red_check_fn=_red, run_fn=_run,
                git=_git), calls


async def _handle(payload, tmp_path, **fake_over):
    gh = _Gh()
    fakes, calls = _fakes(**fake_over)
    (tmp_path / "tests").mkdir(exist_ok=True)
    res = await handle(payload, tmp_path, AifixConfig(), gh, **fakes)
    return res, gh, calls


# ---------------------------------------------------------------- 授权分流

async def test_a_plain_comment_does_nothing_at_all(tmp_path):
    """不是命令 —— 一声不吭，连 reaction 都不加。仓库里每条闲聊都被机器人
    回一句，比不回还糟。"""
    res, gh, _ = await _handle(_payload("我也遇到了"), tmp_path)
    assert res.exit_code == 0 and res.path == "ignored"
    assert not gh.comments and not gh.statuses and not gh.reactions


async def test_an_unauthorized_command_is_answered(tmp_path):
    """看起来是命令但没权限 —— 必须回帖说明。静默丢弃会让人以为它在跑了。"""
    p = _payload()
    p["comment"]["author_association"] = "CONTRIBUTOR"
    res, gh, _ = await _handle(p, tmp_path)
    assert res.exit_code == 0 and res.path == "refused"
    assert gh.comments and "权限" in gh.comments[0][1]


async def test_an_authorized_command_acks_immediately(tmp_path):
    """Actions 从排队到真正开跑有几十秒空窗，这段时间里人要知道命令被听见了。"""
    res, gh, _ = await _handle(_payload(), tmp_path)
    assert gh.reactions and gh.reactions[0][0] == _RAW["payload"]["comment"]["id"]


# ---------------------------------------------------------------- 通路一

async def test_giving_up_only_comments_and_never_opens_a_pr(tmp_path):
    """写不出复现是一条 triage 结论，不是代码产出 —— 没有分支可交付。"""
    out = ReproduceOutcome(
        Reproduction(can_reproduce=False, missing_info=["没说触发的输入"]),
        reason="信息不足，还缺：\n  - 没说触发的输入")
    res, gh, calls = await _handle(_payload(), tmp_path, repro=out)
    assert res.exit_code == 0 and res.path == "no_repro"
    assert gh.comments and "没说触发的输入" in gh.comments[-1][1]
    assert not gh.prs
    assert not any("push" in c for c in calls["git"])


async def test_a_test_that_is_not_red_is_also_no_repro(tmp_path):
    """写出来了但红检不过 —— 一条不红的测试不是复现。

    这一条与上一条走同一个出口，但原因必须原样带出来：人看到「没有失败」和
    看到「收集错误」时，下一步动作完全不同。
    """
    res, gh, _ = await _handle(
        _payload(), tmp_path, red=(False, "在当前代码上**没有失败**"))
    assert res.path == "no_repro"
    assert "没有失败" in gh.comments[-1][1]
    assert not gh.prs


# ---------------------------------------------------------------- 通路二/三

async def test_a_fix_is_delivered_as_a_pr(tmp_path):
    res, gh, calls = await _handle(_payload(), tmp_path)
    assert res.exit_code == 0 and res.path == "delivered"
    assert gh.prs and gh.prs[0]["head"] == "aifix/deadbeef"
    assert "修复 1/1" in gh.prs[0]["body"]
    assert res.pr_url


async def test_the_repro_test_is_committed_before_the_loop_runs(tmp_path):
    """复现测试必须先进 HEAD：run_once 从 HEAD 建 worktree，baseline 才认得出
    它是个失败用例。顺序错了，队列是空的，run 以「没活干」正常收场。"""
    _, _, calls = await _handle(_payload(), tmp_path)
    # 按「有没有这个动作」找，不按 argv 下标 —— 提交要带 -c 署名前缀，
    # 写死 c[0] 会让这条断言变成对参数顺序的断言
    committed = [i for i, c in enumerate(calls["git"]) if "commit" in c]
    pushed = [i for i, c in enumerate(calls["git"]) if "push" in c]
    assert committed and pushed and committed[0] < pushed[0]


async def test_an_unfixed_bug_still_gets_a_pr_but_says_so(tmp_path):
    """一条红着的复现测试本身就是产出 —— 人可以直接接手。丢掉它等于丢掉
    这次 run 里唯一有价值的东西。"""
    st = _state(report_md="# 报告\n\n修复 0/1",
                results=[{"test_id": "tests/test_issue_1.py::test_x",
                          "verdict": "same", "attempts": 3,
                          "abort_reason": "max_attempts"}])
    res, gh, _ = await _handle(_payload(), tmp_path, state=st)
    assert res.path == "delivered" and gh.prs
    assert "未修复" in gh.prs[0]["title"]


async def test_a_fixed_bug_does_not_say_unfixed(tmp_path):
    """反向对照：标题里那个标记必须真的随判定变，不能是常量。"""
    res, gh, _ = await _handle(_payload(), tmp_path)
    assert "未修复" not in gh.prs[0]["title"]


# ---------------------------------------------------------------- baseline 杂音

async def test_other_baseline_failures_are_surfaced_in_the_pr(tmp_path):
    """baseline 里有别的红不中止 —— 那多半是 runner 的环境漂移，硬中止会因为
    一个抖动的用例把整次 run 毁掉。但它会污染「这个补丁没弄坏别的」这个判断，
    所以必须在 PR 正文里出声。"""
    st = _state(baseline_ids=["tests/test_issue_1.py::test_x",
                              "tests/test_other.py::test_y"])
    res, gh, _ = await _handle(_payload(), tmp_path, state=st)
    assert "tests/test_other.py::test_y" in gh.prs[0]["body"]


async def test_a_clean_baseline_adds_no_noise_section(tmp_path):
    """反向对照：没有别的红时不该凭空多出一段告警。"""
    res, gh, _ = await _handle(_payload(), tmp_path)
    assert "baseline" not in gh.prs[0]["body"].lower()


# ---------------------------------------------------------------- 崩溃

async def test_a_crash_exits_nonzero_and_still_reports(tmp_path):
    """只有崩溃才让 job 红。写不出复现、没修好都是正常结论 —— 让它们退非 0
    的话，Actions 页面会满屏红叉，而其中大半根本不是错误。"""
    st = _state(abort_kind="crash", report_md="# 报告\n\n运行异常中断")
    res, gh, _ = await _handle(_payload(), tmp_path, state=st)
    assert res.exit_code == 1 and res.path == "crashed"
    assert gh.statuses and "异常" in gh.statuses[-1][1]


# ---------------------------------------------------------------- CLI 入口

def test_issue_handle_is_wired_into_the_dispatch_table():
    """加了 parser 却忘了接分派表：argparse 解析成功、什么都不做、退出码 0。"""
    from aifix.cli import _dispatch, build_parser
    a = build_parser().parse_args(["issue", "handle"])
    assert a.cmd == "issue" and a.issue_cmd == "handle"
    assert "issue" in _dispatch()


def test_event_path_defaults_to_the_actions_env(monkeypatch):
    """Actions 把载荷路径写在 GITHUB_EVENT_PATH 里。要显式传的话，workflow
    每次都得重复一遍那个变量名 —— 而写错了不会报错，只会读不到文件。"""
    from aifix.cli import build_parser
    monkeypatch.setenv("GITHUB_EVENT_PATH", "/tmp/ev.json")
    assert build_parser().parse_args(["issue", "handle"]).event == "/tmp/ev.json"


def test_missing_event_path_is_a_readable_error(tmp_path, monkeypatch, capsys):
    """本地调试忘了设那个变量是最常见的第一次失败。裸 FileNotFoundError 只会
    说某个路径不存在，一个字都不提示它本该由 Actions 提供。"""
    from aifix.cli import _cmd_issue, build_parser
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    args = build_parser().parse_args(
        ["issue", "handle", "--repo", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        _cmd_issue(args)
    assert e.value.code == 1
    assert "GITHUB_EVENT_PATH" in capsys.readouterr().out


# ---------------------------------------------------------------- trace 归档

async def test_the_trace_is_archived_after_a_delivery(tmp_path):
    seen = []
    fakes, _ = _fakes()
    gh = _Gh()
    (tmp_path / "tests").mkdir(exist_ok=True)

    def _pub(repo, run_id, **k):
        seen.append(run_id)
        return True

    await handle(_payload(), tmp_path, AifixConfig(), gh, publish=_pub, **fakes)
    assert seen, "run 结束后没有归档 trace —— runner 上它会随机器一起消失"
    assert "trace" in gh.statuses[-1][1]


async def test_a_failed_archive_does_not_break_the_delivery(tmp_path):
    """补丁已经推上去、PR 已经开了。为一次归档失败把 job 弄红，等于让人
    以为修复没成功 —— 而它成功了。"""
    fakes, _ = _fakes()
    gh = _Gh()
    (tmp_path / "tests").mkdir(exist_ok=True)

    def _boom(repo, run_id, **k):
        raise RuntimeError("远端拒绝了")

    res = await handle(_payload(), tmp_path, AifixConfig(), gh,
                       publish=_boom, **fakes)
    assert res.exit_code == 0 and res.path == "delivered"
    assert gh.prs, "PR 仍然要开出来"
    assert "归档失败" in gh.statuses[-1][1], "但必须出声"
