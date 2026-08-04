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


# ---------------------------------------------------------------- 收拾与兜底

async def test_a_failed_red_check_does_not_leave_the_test_behind(tmp_path):
    """红检不过就得把写下去的文件收走。

    留着的后果不是「多个文件」：它是一条**红着的**测试，下一次 run 的 baseline
    会把它算进失败集，于是模型被派去修一个上一次已经判定为无效的复现。
    """
    # 落盘走的是真的 write_reproduction（handle 只打桩了生成与红检），
    # 所以这条断言量的是「写下去之后有没有被收走」，不是「有没有写过」。
    _, _, _ = await _handle(_payload(), tmp_path, red=(False, "没有失败"))
    assert not (tmp_path / "tests" / "test_issue_1.py").exists()

    # 反向对照：红检通过时它必须还在 —— 否则上一条断言恒真
    _, _, _ = await _handle(_payload(), tmp_path)
    assert (tmp_path / "tests" / "test_issue_1.py").exists()


async def test_an_abort_before_the_worktree_exists_still_reports(tmp_path):
    """preflight 阶段就中止时没有交付分支 —— 不能拿空分支名去 push。

    裸抛的后果不是「报错」而是失联：没有 PR、没有状态评论、issue 里最后一条
    还停在那个 👀，人只能去 Actions 页面读一段调用栈。而 run_once 已经把报告
    准备好了，那里面写着到底出了什么事。
    """
    st = _state(branch="", abort_kind="model",
                report_md="# 报告\n\n模型端点不可达")
    res, gh, calls = await _handle(_payload(), tmp_path, state=st)
    assert not any("push" in c for c in calls["git"]), "没有分支可推"
    assert not gh.prs, "没有分支就没有 PR"
    assert gh.comments and "模型端点不可达" in gh.comments[-1][1]
    assert res.path == "aborted"


async def test_a_push_failure_is_reported_not_thrown(tmp_path):
    """推不上去（没配远端、认证过期）同样不能裸抛。

    与空分支那条是同一个失联：异常穿出去 → 没有 PR、没有说明、issue 里最后
    一条还停在 👀。区别只在这次**确实是个失败**，所以退非 0。
    """
    gh = _Gh()
    fakes, calls = _fakes()

    def _git(repo, *args):
        calls["git"].append(list(args))
        if "push" in args:
            raise RuntimeError("remote rejected")
        return ""

    fakes["git"] = _git
    (tmp_path / "tests").mkdir(exist_ok=True)
    res = await handle(_payload(), tmp_path, AifixConfig(), gh, **fakes)
    assert res.exit_code == 1 and res.path == "push_failed"
    assert not gh.prs
    assert gh.comments and "remote rejected" in gh.comments[-1][1]


async def test_environment_aborts_exit_nonzero(tmp_path):
    """口径要和 `aifix run` 一致：crash / collect / model 三种都是「这次没跑
    成」，退 0 会让流水线把它读成正常收场。

    反向对照：预算耗尽是**正常收场**（活干到钱花完为止，结论仍可信），退 0。
    """
    for kind, code in (("crash", 1), ("collect", 1), ("model", 1),
                       ("cny", 0), ("wall", 0), (None, 0)):
        st = _state(branch="", abort_kind=kind)
        res, _, _ = await _handle(_payload(), tmp_path, state=st)
        assert res.exit_code == code, f"{kind} 应该退 {code}"


# ---------------------------------------------------------------- 预算

async def test_the_reproduce_spend_is_deducted_from_the_run_budget(tmp_path):
    """复现那一步花的钱必须从后面的额度里扣掉。

    它在 `run_once` **之外**发起调用，三层预算闸一分都管不到。不扣的话，
    设 AIFIX_BUDGET_CNY=0.50 实际可能花掉两倍 —— 而这个项目对预算的措辞是
    「越线之后不再发起新的模型调用」，超支上界必须是可推导的。一个精确措辞
    但从没验证过的上界实际超支 4 倍，是这个仓库已经犯过的错。
    """
    seen = {}
    fakes, _ = _fakes(repro=ReproduceOutcome(_REPRO, tokens=1200,
                                             cost_cny=0.30))

    async def _run(repo, config, **k):
        seen["cny"] = config.budget_cny
        seen["tokens"] = config.budget_tokens
        return _state()

    fakes["run_fn"] = _run
    cfg = AifixConfig(budget_cny=0.50, budget_tokens=10_000)
    (tmp_path / "tests").mkdir(exist_ok=True)
    await handle(_payload(), tmp_path, cfg, _Gh(), **fakes)
    assert seen["cny"] == pytest.approx(0.20)
    assert seen["tokens"] == 8_800


async def test_the_budget_never_goes_negative(tmp_path):
    """复现就把额度花超时，给 run_once 的是 0 而不是负数。

    负数会让「还剩多少」的比较全部反向 —— 那时闸最该拦住的一刻，恰好完全不拦。
    """
    seen = {}
    fakes, _ = _fakes(repro=ReproduceOutcome(_REPRO, tokens=99_999,
                                             cost_cny=9.9))

    async def _run(repo, config, **k):
        seen["cny"], seen["tokens"] = config.budget_cny, config.budget_tokens
        return _state()

    fakes["run_fn"] = _run
    (tmp_path / "tests").mkdir(exist_ok=True)
    await handle(_payload(), tmp_path,
                 AifixConfig(budget_cny=0.50, budget_tokens=10_000),
                 _Gh(), **fakes)
    assert seen["cny"] == 0.0 and seen["tokens"] == 0


async def test_the_reproduce_cost_is_visible_in_the_pr(tmp_path):
    """扣掉还不够，还得让人看见 —— 报告里的成本只算 run_once 那一段，
    PR 上不写的话，这笔钱在任何一份产物里都不存在。"""
    fakes, _ = _fakes(repro=ReproduceOutcome(_REPRO, tokens=1200,
                                             cost_cny=0.30))
    gh = _Gh()
    (tmp_path / "tests").mkdir(exist_ok=True)
    await handle(_payload(), tmp_path, AifixConfig(), gh, **fakes)
    assert "1,200" in gh.prs[0]["body"] or "1200" in gh.prs[0]["body"]


def test_issue_handle_refuses_a_budget_without_a_price_map(
        tmp_path, monkeypatch):
    """与 `aifix run` 同一道闸。在 Actions 上这个假保证尤其贵：没人盯着终端，
    发现时钱已经花完了。"""
    from aifix.cli import _cmd_issue, build_parser
    monkeypatch.setenv("AIFIX_BUDGET_CNY", "3.5")
    monkeypatch.delenv("AIFIX_PRICE_MAP", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "ev.json"))
    args = build_parser().parse_args(
        ["issue", "handle", "--repo", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        _cmd_issue(args)
    assert "价格表" in str(e.value)


async def test_the_wall_clock_spent_on_reproduce_is_deducted_too(tmp_path):
    """墙钟与金额、token 同理 —— 复现那一步耗掉的时间也不在闸内。

    不扣的话，workflow 里那条不变式会被击穿：`AIFIX_BUDGET_WALL_SECONDS=3600`
    配 `timeout-minutes: 90`（5400 秒），复现 3600 + 修复 3600 = 7200 > 5400，
    **软闸赶不在硬杀前面**。而硬杀是直接杀进程：run_once 里那个「保证报告先
    落地」的 except 执行不到，跑了一个半小时什么都留不下。
    """
    import asyncio

    seen = {}

    async def _reproduce(*a, **k):
        await asyncio.sleep(0.05)
        return ReproduceOutcome(_REPRO, tokens=5)

    async def _red(*a, **k):
        # 红检跑的是真测试，同样要计入 —— 只掐模型那一段会漏掉一大半
        await asyncio.sleep(0.05)
        return True, ""

    async def _run(repo, config, **k):
        seen["wall"] = config.budget_wall_seconds
        return _state()

    fakes, _ = _fakes()
    fakes["reproduce_fn"], fakes["red_check_fn"], fakes["run_fn"] = (
        _reproduce, _red, _run)
    cfg = AifixConfig(budget_wall_seconds=100.0)
    (tmp_path / "tests").mkdir(exist_ok=True)
    await handle(_payload(), tmp_path, cfg, _Gh(), **fakes)
    # 两段各 0.05 秒都得算进去。只扣模型那一段的话，这里会 > 99.95
    assert seen["wall"] < 99.9, "复现与红检耗掉的墙钟没有从后面的额度里扣"
    assert seen["wall"] >= 99.0, "扣多了 —— 只该扣真正花掉的那点"


# ---------------------------------------------------------------- 失败措辞与 trace

async def test_no_convergence_is_not_worded_as_a_vague_issue(tmp_path):
    """步数耗尽时**不能**让人去改 issue —— 改多少遍都没用。

    这是 2026-07-30 第一次真跑（issue #1）撞出来的：回帖说「没能写出复现测试」，
    而真相是模型翻了 25 步没作答。两者的下一步动作完全相反。
    """
    from aifix.reproduce import KIND_NO_CONVERGENCE
    out = ReproduceOutcome(None, "模型翻了 25 步仍未给出复现测试",
                           kind=KIND_NO_CONVERGENCE)
    res, gh, _ = await _handle(_payload(), tmp_path, repro=out)
    body = gh.comments[-1][1]
    assert res.path == "no_repro"
    # **标题本身**要分得开。恒定的「没能写出复现测试」正是那次误导的来源：
    # 它读起来像「你的 issue 不够清楚」，而真相是系统这边该调参数。
    head = body.splitlines()[0]
    assert "没能写出复现测试" not in head, f"标题没有区分度：{head}"
    assert "收敛" in head


async def test_missing_info_is_worded_as_a_request_to_the_human(tmp_path):
    """反向对照：真的是信息不足时，措辞就该指向人去补 issue。"""
    from aifix.agents.reproducer import Reproduction
    from aifix.reproduce import KIND_MISSING_INFO
    out = ReproduceOutcome(
        Reproduction(can_reproduce=False, missing_info=["没说触发的输入"]),
        reason="issue 里的信息不足以写出复现测试，还缺：\n  - 没说触发的输入",
        kind=KIND_MISSING_INFO)
    res, gh, _ = await _handle(_payload(), tmp_path, repro=out)
    head = gh.comments[-1][1].splitlines()[0]
    assert "信息不足" in head, f"标题没指向人去补 issue：{head}"


async def test_the_reproduce_events_are_written_to_a_trace(tmp_path):
    """复现这一步的事件必须落盘，**哪怕后面根本走不到 run_once**。

    第一次真跑时这条通路没有任何 trace（RunTrace 建在 run_once 里），artifact
    是空的 —— 于是「模型这 25 步在读什么」这个唯一有诊断价值的问题无从回答。
    失败时恰恰最需要它。
    """
    out = ReproduceOutcome(None, "翻不出来", kind="no_convergence",
                           tokens=42, events=[{"fake": "event"}])
    await _handle(_payload(), tmp_path, repro=out)
    runs = list((tmp_path / ".aifix" / "runs").glob("*/events.jsonl"))
    assert runs, "没有留下任何 trace"
    assert (runs[0].parent / "facts.jsonl").is_file()


async def test_a_pr_creation_failure_is_reported_not_thrown(tmp_path):
    """开 PR 失败同样不能裸抛 —— 分支**已经推上去了**，成果还在。

    实测（2026-07-30，issue #2）：`gh pr create` 撞上仓库设置
    「Allow GitHub Actions to create and approve pull requests」默认关闭，
    异常穿出去 → job 红、issue 上一条帖都没有、人不知道分支叫什么。
    而那条分支上躺着一条可用的复现测试。

    上一轮只包了 push，漏了这一步 —— 同一类失联的第二处。
    """
    class _Boom(_Gh):
        def create_pr(self, head, title, body, base=None):
            raise RuntimeError(
                "GitHub Actions is not permitted to create or approve pull requests")

    gh = _Boom()
    fakes, _ = _fakes()
    (tmp_path / "tests").mkdir(exist_ok=True)
    res = await handle(_payload(), tmp_path, AifixConfig(), gh, **fakes)
    assert res.exit_code == 1 and res.path == "pr_failed"
    body = gh.comments[-1][1]
    # 分支名必须在里面：那是人接手的唯一入口
    assert "aifix/deadbeef" in body
    # 而且要指出**具体那个设置**，不是一句「开 PR 失败了」
    assert "create and approve pull requests" in body


async def test_the_core_loop_gets_a_progress_reporter(tmp_path):
    """issue 那条路必须把进度接上 —— 否则 Actions 日志里是**几十分钟的空屏**。

    实测（2026-08-03，issue #9）那一步的日志只有两行：

        03:58:34  env 声明
        04:27:01  通路：delivered · PR：…

    中间 28 分半，**零行输出**。而命令行那条路（`aifix run`）一直有
    TerminalProgress 的逐步心跳 —— 只有 issue 这条漏了，于是它卡住的时候
    没有任何办法判断卡在哪。

    钉的是「传了一个会出声的 progress」，不是具体措辞（措辞归 progress.py）。
    """
    import io

    seen = {}

    async def _run(*a, progress=None, **k):
        seen["progress"] = progress
        return _state()

    res, gh, _ = await _handle(_payload(), tmp_path, state=None)
    # 上面那次只是把夹具跑通；下面这次专门抓 progress
    (tmp_path / "tests").mkdir(exist_ok=True)
    fakes, _ = _fakes()
    fakes["run_fn"] = _run
    await handle(_payload(), tmp_path, AifixConfig(), _Gh(), **fakes)

    prog = seen.get("progress")
    assert prog is not None, "run_once 拿到的 progress 是 None —— 又是空屏"

    # 断言它**真的会出声**，而不是 `not isinstance(prog, NullProgress)`：
    # TerminalProgress 继承自 NullProgress，那个判据恒为 False —— 一条永远
    # 通不过的断言和一条永远通过的一样没用。直接让它说一句，看有没有字出来。
    buf = io.StringIO()
    prog._stream = buf
    prog.note("测试心跳")
    assert buf.getvalue().strip(), "传进去的 progress 一个字都不印"


def test_the_old_usd_budget_variable_still_arms_the_same_gate(
        tmp_path, monkeypatch):
    """`AIFIX_BUDGET_USD` 是旧名，仍按**美元**读并折成人民币。

    两件事都要钉住：折算过的值确实进了 budget_cny（不是被当成人民币直接
    用，那是一次静默的 7 倍缩水），以及它照样算「用户显式要了一道闸」——
    不算的话，只设了旧变量的仓库会绕过「没有价格表就拒绝启动」那一条，
    于是那个假保证悄悄回来了。
    """
    from aifix.cli import _cmd_issue, build_parser
    monkeypatch.setenv("AIFIX_BUDGET_USD", "0.5")
    monkeypatch.delenv("AIFIX_BUDGET_CNY", raising=False)
    monkeypatch.delenv("AIFIX_PRICE_MAP", raising=False)

    cfg = AifixConfig()
    assert cfg.budget_cny == pytest.approx(0.5 * cfg.usd_to_cny)

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "ev.json"))
    args = build_parser().parse_args(
        ["issue", "handle", "--repo", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        _cmd_issue(args)
    assert "价格表" in str(e.value)
