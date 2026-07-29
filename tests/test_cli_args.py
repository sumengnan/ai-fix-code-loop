import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import build_parser, run_once
from aifix.config import AifixConfig

# argparse（3.13+）会给 usage / 小节标题 / 选项名上色。断言的是正文里的中文，
# 一般碰不到，但把控制序列剥掉才算真的只对内容断言。
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_run_accepts_repo():
    args = build_parser().parse_args(["run", "/tmp/x"])
    assert args.repo == "/tmp/x"
    assert args.cmd == "run"


def test_run_repo_defaults_to_cwd():
    assert build_parser().parse_args(["run"]).repo == "."


def test_budget_override():
    args = build_parser().parse_args(["run", "--budget", "0.5"])
    assert args.budget == 0.5


def test_budget_defaults_to_none():
    """没给 --budget 就不该覆盖配置里的值。"""
    assert build_parser().parse_args(["run"]).budget is None


def test_test_filter():
    args = build_parser().parse_args(["run", "--test", "tests/x.py::y"])
    assert args.test == "tests/x.py::y"


def test_dry_run_flag():
    assert build_parser().parse_args(["run", "--dry-run"]).dry_run is True
    assert build_parser().parse_args(["run"]).dry_run is False


def test_unknown_subcommand_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nope"])


def test_mine_subcommand():
    a = build_parser().parse_args(
        ["mine", "/tmp/proj", "--limit", "80", "--max-tasks", "5",
         "--out", "e/t.jsonl"])
    assert a.cmd == "mine"
    assert a.repo == "/tmp/proj"
    assert a.limit == 80
    assert a.max_tasks == 5
    assert a.out == "e/t.jsonl"


def test_mine_defaults():
    a = build_parser().parse_args(["mine"])
    assert a.repo == "."
    assert a.limit == 50
    assert a.max_tasks == 10


def test_eval_subcommand():
    a = build_parser().parse_args(
        ["eval", "e/t.jsonl", "--parallel", "8", "--label", "pro",
         "--out", "e/r.jsonl"])
    assert a.cmd == "eval"
    assert a.tasks == "e/t.jsonl"
    assert a.parallel == 8
    assert a.label == "pro"


def test_eval_requires_task_file():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval"])


def test_eval_report_takes_many_result_files():
    a = build_parser().parse_args(["eval-report", "a.jsonl", "b.jsonl"])
    assert a.cmd == "eval-report"
    assert a.results == ["a.jsonl", "b.jsonl"]


def test_eval_report_requires_at_least_one():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval-report"])


def test_safe_label_replaces_slashes():
    """含斜杠的 label 被洗成不含路径分隔符的单段文件名。"""
    from aifix.cli import _safe_label
    assert _safe_label("org/model-v2") == "org_model-v2"
    assert _safe_label("deepseek-ai/deepseek-coder") == "deepseek-ai_deepseek-coder"


def test_safe_label_keeps_normal_names():
    """不含特殊字符的普通 label 保持原样。"""
    from aifix.cli import _safe_label
    assert _safe_label("gpt-4") == "gpt-4"
    assert _safe_label("claude_sonnet") == "claude_sonnet"
    assert _safe_label("model.v2") == "model.v2"


def test_safe_label_fallback_for_empty():
    """洗完为空的极端输入有兜底。"""
    from aifix.cli import _safe_label
    assert _safe_label("///") == "未命名"
    assert _safe_label("   ") == "未命名"
    assert _safe_label("") == "未命名"


def test_explicit_usd_budget_without_price_map_is_refused():
    """设了上限却没价格表 = 一个系统给不了的保证。当场拒绝。"""
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    cfg = AifixConfig(budget_usd=0.6)          # 显式提供
    with pytest.raises(SystemExit) as e:
        require_price_map_for_usd_budget(cfg)
    msg = str(e.value)
    assert "AIFIX_PRICE_MAP" in msg, "报错要说清楚缺什么、怎么配"


def test_default_usd_budget_without_price_map_is_allowed():
    """没显式要求就不打扰 —— 退回 token 闸。"""
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    require_price_map_for_usd_budget(AifixConfig())   # 不抛即通过


def test_explicit_usd_budget_with_price_map_is_allowed():
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    cfg = AifixConfig(budget_usd=0.6, price_map={"m": [0.001, 0.002]})
    require_price_map_for_usd_budget(cfg)             # 不抛即通过


def test_cli_budget_flag_counts_as_explicit():
    """--budget 走的是 model_copy，也会被 model_fields_set 记住。"""
    from aifix.config import AifixConfig

    cfg = AifixConfig().model_copy(update={"budget_usd": 0.6})
    assert "budget_usd" in cfg.model_fields_set


def test_eval_budget_flags():
    a = build_parser().parse_args(
        ["eval", "t.jsonl", "--budget-per-task", "0.5", "--budget-total", "5"])
    assert a.budget_per_task == 0.5
    assert a.budget_total == 5.0


def test_eval_budget_flags_default_to_none():
    a = build_parser().parse_args(["eval", "t.jsonl"])
    assert a.budget_per_task is None
    assert a.budget_total is None


def _sub_help(name: str) -> str:
    """取某个子命令的帮助文本。

    argparse 没有公开的取法，只能从 actions 里找 _SubParsersAction；
    按类型找而不是按下标取，子命令增减时不会错位。

    归一化掉**全部**空白：argparse 按终端宽度重排换行，不归一化的话断言
    会随 COLUMNS 变化时红时绿 —— 一个守契约的测试自己按终端宽度飘。
    实测 `COLUMNS=45` 时下面的契约断言当场变红，而契约一个字没改。

    为什么是「删掉空白」而不是「换行折成空格」（`" ".join(split())`）：
    帮助文本是中文，整段之间没有空格，textwrap 只能按 break_long_words
    在**任意位置**硬断。实测 COLUMNS=45 断在「不再发 起新的模型调用」中
    间——折成空格后那个空格留在词里，断言照样红。折成空格只是把「随宽度
    飘」的窗口变窄，没有关掉它。所以断言里的字符串也一律不带空格写。
    """
    import argparse
    for act in build_parser()._actions:
        if isinstance(act, argparse._SubParsersAction):
            return "".join(_ANSI.sub("", act.choices[name].format_help()).split())
    raise AssertionError("没有找到子命令解析器")


def test_run_budget_help_states_the_contract():
    """契约必须出现在 --help 里：越线之后不再发起新调用，不是不超一分钱。

    用户有权知道这个保证的边界在哪儿，而不是超支之后才发现。
    """
    assert "不再发起新的模型调用" in _sub_help("run")


def test_eval_total_help_states_the_overshoot_bound():
    """双向钉：正确说法必须在，被证伪的旧说法必须回不来。

    只断言「并发数」三个字区分度太弱 —— 旧说法「并发数 - 1 个任务」和
    更正后的「并发数 × 一次模型调用」都含这三个字，测试对这次更正毫无
    反应。旧说法是实测证伪的：total_usd=1.0、每任务 1.0、4 个任务、
    parallel=4，按它算应该只花 $1.0，实际花掉 $4.00 且 4 个任务全跑满。
    """
    h = _sub_help("eval")            # 已删掉全部空白，断言串也不带空格
    assert "并发数×一次模型调用" in h, "超支上界要写进 --help"
    for stale in ("并发数-1", "并发数−1"):
        assert stale not in h, f"「{stale}」是 parallel=4 实测 4 倍超支证伪掉的旧说法"


def test_mutate_subcommand_exists_and_states_its_positioning():
    """帮助文本必须说清这是冒烟集不是基准 —— 否则这些数字会被当成结论。"""
    text = _sub_help("mutate")     # 既有 helper，已剥 ANSI 且删掉全部空白
    assert "冒烟集" in text
    assert "不是基准" in text


def test_mutate_flags():
    args = build_parser().parse_args(
        ["mutate", ".", "--max-tasks", "3", "--scope", "full"])
    assert args.max_tasks == 3 and args.scope == "full"


def test_mutate_defaults():
    a = build_parser().parse_args(["mutate"])
    assert a.repo == "."
    assert a.max_tasks == 10
    assert a.max_new_failures == 5
    assert a.scope == "smart"
    assert a.seed == 0
    assert a.out == "evals/tasks-mutants.jsonl"


def test_cmd_mutate_salvages_tasks_and_fails_with_a_readable_message(
        tmp_path, monkeypatch, capsys):
    """撞车时：人话 + 已验证的任务落盘 + 主输出保持不存在。

    落到 args.out 的话，用户下一步 `aifix eval` 会拿到一个撞车的任务集 ——
    正是这道自检要挡的事。所以旁落一份 .partial，主输出一个字节都不写。
    """
    from aifix import cli as cli_mod
    from aifix.eval.mutate import DuplicateTaskIds
    from aifix.eval.task import Task

    salvaged = [Task(task_id="p@mut::calc.py:1::x::t", repo=str(tmp_path),
                     commit="a" * 40, base_commit="a" * 40, test_files=[],
                     target_test="t", gold_files=["calc.py"],
                     adapter="pytest", origin="mutated")]

    async def boom(*a, **kw):
        raise DuplicateTaskIds("变异产出了重复的 task_id：dup-1",
                               tasks=salvaged, duplicates=["dup-1"])

    monkeypatch.setattr("aifix.eval.mutate.mutate_tasks", boom)
    out = tmp_path / "tasks.jsonl"
    args = cli_mod.build_parser().parse_args(
        ["mutate", str(tmp_path), "--out", str(out)])

    with pytest.raises(SystemExit) as exc:
        cli_mod._cmd_mutate(args)

    msg = str(exc.value)
    assert "dup-1" in msg, "人话里要点名撞车的 id"
    assert "Traceback" not in msg
    partial = tmp_path / "tasks.jsonl.partial"
    assert partial.is_file(), "已验证的任务必须落盘，不能白丢"
    assert "p@mut::calc.py:1::x::t" in partial.read_text(encoding="utf-8")
    assert str(partial) in msg, "要告诉用户捞出来的那份在哪"
    assert not out.exists(), "撞车的任务集绝不能落在用户会拿去 eval 的路径上"


# ======== 诊断三件套：replay / ingest / stats ========

def test_replay_subcommand_flags():
    a = build_parser().parse_args(
        ["replay", "abc123", "--repo", "/tmp/p", "--step", "3", "--full"])
    assert a.cmd == "replay"
    assert a.run_id == "abc123"
    assert a.repo == "/tmp/p"
    assert a.step == 3
    assert a.full is True


def test_replay_defaults():
    a = build_parser().parse_args(["replay", "abc123"])
    assert a.repo == "."
    assert a.step is None
    assert a.full is False


def test_replay_requires_run_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay"])


def test_ingest_and_stats_default_to_cwd():
    assert build_parser().parse_args(["ingest"]).repo == "."
    assert build_parser().parse_args(["stats"]).repo == "."


def test_every_subcommand_is_wired_to_a_handler():
    """加了 parser 却忘了接分派表：argparse 解析成功、什么都不做、退出码 0。"""
    import argparse

    from aifix.cli import _dispatch

    for act in build_parser()._actions:
        if isinstance(act, argparse._SubParsersAction):
            assert set(_dispatch()) == set(act.choices)
            break
    else:
        raise AssertionError("没有找到子命令解析器")


def test_replay_help_states_the_step_numbering_and_the_not_found_behaviour():
    """步号口径必须写进帮助：一次 run 会开好几段 AgentLoop，每段内部的步号
    都从 1 重新数，`--step 2` 指的是重新编过的全局步号，不是某一段里的第 2 步。
    """
    h = _sub_help("replay")          # 既有 helper，已剥 ANSI 且删掉全部空白
    assert "全局步号" in h
    assert "列出这个仓库里现有的run" in h, "找不到 run 时要指路，这得写在帮助里"


def test_ingest_help_states_idempotence():
    """幂等是这个命令的核心契约：不写进帮助，用户不敢重灌，也不知道重灌安全。"""
    h = _sub_help("ingest")
    assert "灌任意多次" in h
    assert "行数不变" in h


def test_stats_help_warns_that_an_empty_table_is_not_an_empty_history():
    """空表只说明没灌过库。不写这一条，用户会把「没数据」读成「没跑过 run」。"""
    assert "空表不代表没跑过" in _sub_help("stats")


# ---- 下面几条要真产物 ----
#
# 手写 facts.jsonl 只能证明我们对格式的理解自洽，证明不了 CLI 渲染出来的
# 东西真的存在于产物里。所以跟 test_e2e / test_trajectory 一样，用脚本化
# 假客户端真跑几次 run_once（module 级夹具，整个文件只跑一次）。

# calc.py 里多一个 mul：让「补丁删掉公开符号」这条信号有机会真的被触发。
_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b


def mul(a, b):
    return a * b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_identity():
    assert add(0, 0) == 0
'''

_PATCH_DROPS_MUL = """--- a/calc.py
+++ b/calc.py
@@ -1,6 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
-
-
-def mul(a, b):
-    return a * b
+    return a + b
"""

# ensure_ascii=False：真模型吐的是 UTF-8 原文。用默认的 ensure_ascii 会让
# 「某段中文不在输出里」这类断言在转义形式下恒真。
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
}, ensure_ascii=False)


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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    repo = root / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


async def _three_runs(repo: Path) -> None:
    """三次形状不同的真 run：修好并留下信号 / 撞空补丁守卫 / 撞超大补丁守卫。

    两种守卫的次数必须**不相等**，否则「按次数降序」的断言恒真。
    """
    await run_once(repo, AifixConfig(), run_id="r_fix",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch",
                             json.dumps({"diff": _PATCH_DROPS_MUL})),
                       _text("已修复")]))
    await run_once(repo, AifixConfig(), run_id="r_guard",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([_text("我什么都不做")]))
    await run_once(repo, AifixConfig(max_diff_lines=1, max_attempts=1),
                   run_id="r_huge",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch",
                             json.dumps({"diff": _PATCH_DROPS_MUL})),
                       _text("已修复")]))


@pytest.fixture(scope="module")
def real_runs(tmp_path_factory) -> Path:
    repo = _make_repo(tmp_path_factory.mktemp("cli"))
    asyncio.run(_three_runs(repo))
    return repo / ".aifix" / "runs"


@pytest.fixture
def repo(tmp_path: Path, real_runs: Path) -> Path:
    """每个用例一份独立仓库：db 落在仓库里，共用会让用例之间隔空传状态。"""
    dst = tmp_path / "repo"
    (dst / ".aifix").mkdir(parents=True)
    shutil.copytree(real_runs, dst / ".aifix" / "runs")
    return dst


def _run_cmd(argv: list[str]) -> None:
    from aifix import cli as cli_mod

    args = cli_mod.build_parser().parse_args(argv)
    cli_mod._dispatch()[args.cmd](args)


def _count(repo: Path, table: str) -> int:
    with sqlite3.connect(repo / ".aifix" / "trajectory.db") as con:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_replay_unknown_run_id_lists_what_this_repo_actually_has(repo, capsys):
    """诊断工具的第一要务是让人找得到东西。

    只断言「没崩」是没区分度的：一句「找不到」而不告诉用户现有哪些 run，
    等于让人去 ls 一遍产物目录 —— 那正是这个命令该替人做的事。
    """
    with pytest.raises(SystemExit) as e:
        _run_cmd(["replay", "打错的id", "--repo", str(repo)])
    out = capsys.readouterr().out

    assert "Traceback" not in out
    assert "打错的id" in out
    for run_id in ("r_fix", "r_guard", "r_huge"):
        assert run_id in out, f"现有的 {run_id} 必须被列出来"
    # 人话照常印在 stdout（可 grep、可贴），但退出码得说明这次没找到东西 ——
    # 退 0 的话流水线里「run_id 打错了」和「回放成功」没有区别
    assert e.value.code == 1


def test_replay_renders_the_run_and_never_fakes_a_zero_cost(repo, capsys):
    """没配价格表时成本恒为 0 —— 这个项目为「假的 $0.00」栽过三次。"""
    _run_cmd(["replay", "r_fix", "--repo", str(repo)])
    out = capsys.readouterr().out

    assert "apply_patch" in out
    assert "return a + b" in out, "补丁正文要在，否则复盘看不到改了什么"
    assert "$0.00" not in out
    assert "未配置 AIFIX_PRICE_MAP" in out


def test_replay_step_flag_is_wired_through(repo, capsys):
    """--step 没接上时输出的是完整时间轴，一样「不报错」。"""
    _run_cmd(["replay", "r_fix", "--repo", str(repo)])
    whole = capsys.readouterr().out
    _run_cmd(["replay", "r_fix", "--repo", str(repo), "--step", "2"])
    one = capsys.readouterr().out

    assert whole.count("步骤") > 1
    assert one.count("步骤") == 1
    assert len(one) < len(whole)


def test_ingest_is_idempotent_and_reports_how_many_runs(repo, capsys):
    """第二次灌进去翻倍不报错、不崩溃，只让此后所有聚合数字翻倍。"""
    _run_cmd(["ingest", "--repo", str(repo)])
    first_out = capsys.readouterr().out
    # 整句比，不用「含 3」：这一行里还印着 db 全路径，路径里出现数字 3
    # 的概率不低（tmp 目录名、pytest-of-<user> 下的序号），含子串的取法
    # 在「灌了 999 个」的实现下照样是绿的 —— 实测过。
    assert "已灌库 3 个 run" in first_out, first_out

    facts, runs = _count(repo, "facts"), _count(repo, "runs")
    assert facts > 10, "行数为 0 的话下面「不变」是恒真的"
    assert runs == 3

    _run_cmd(["ingest", "--repo", str(repo)])
    second_out = capsys.readouterr().out
    assert _count(repo, "facts") == facts, "重灌翻倍是这个功能最可能的 bug"
    assert _count(repo, "runs") == runs
    assert "已灌库 3 个 run" in second_out, "重灌报的是本次处理数，不是新增数"


def test_ingest_on_a_repo_without_runs_says_so(tmp_path, capsys):
    _run_cmd(["ingest", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert "没有可灌的 run" in out
    assert "Traceback" not in out


def test_stats_before_ingest_points_at_ingest(tmp_path):
    """空表和「没跑过 run」是两回事，直接渲染一张空表会让人读成后者。"""
    with pytest.raises(SystemExit) as e:
        _run_cmd(["stats", "--repo", str(tmp_path)])
    assert "ingest" in str(e.value)


def test_stats_renders_decoded_values_in_frequency_order(repo, capsys):
    """库里标量是 JSON 文本（`"empty_diff"`，带引号）。直接印出来就是把内部
    编码丢给用户看。
    """
    _run_cmd(["ingest", "--repo", str(repo)])
    capsys.readouterr()
    _run_cmd(["stats", "--repo", str(repo)])
    out = capsys.readouterr().out

    assert '"empty_diff"' not in out, "带引号的值是库里的编码，不是给人看的"
    assert "empty_diff" in out
    # 按次数降序：真产物里 empty_diff 8 次、huge_diff 1 次
    assert out.index("empty_diff") < out.index("huge_diff")
    # 按行首取，不用「含 pytest」：抬头那行印的是 db 全路径，而临时目录
    # 恰好叫 pytest-of-<user>，含子串的取法在「整节被删掉」的实现下照样
    # 是绿的 —— 实测过。
    assert any(ln.startswith("  pytest：run 3 次") for ln in out.splitlines()), \
        f"每个 adapter 的 run 数要能一眼看到：{out}"
    assert "r_fix" in out, "可疑信号最多的 run 是 r_fix（另两个 run 一条都没有）"
    assert "r_guard" not in out.split("信号")[-1], \
        "撞守卫被回滚的 run 一条信号都没有，不该出现在信号榜上"


def test_stats_shows_a_dash_when_the_fixed_count_is_unknown(repo, capsys):
    """「取不到」和「是 0」是两回事。

    修复数取自 run 收尾时落的 fact，老产物里没有这条、只能解 report.md，
    两条都没有时库里是 NULL。渲染成 0 就是在说「这批 run 一个都没修好」——
    那是另一回事，而且看起来完全正常。

    造一个两条都缺的 run：删掉 report.md 已经不够了（fact 还在），而这里
    要测的正是 NULL 这一列怎么渲染。
    """
    old = repo / ".aifix" / "runs" / "r_old"
    old.mkdir()
    (old / "facts.jsonl").write_text(
        json.dumps({"run_id": "r_old", "key": "baseline_failures", "value": 1})
        + "\n", encoding="utf-8")
    _run_cmd(["ingest", "--repo", str(repo)])
    capsys.readouterr()
    _run_cmd(["stats", "--repo", str(repo)])
    out = capsys.readouterr().out

    unknown = [ln for ln in out.splitlines() if "未知" in ln]
    assert unknown, "adapter 解不出来时要显示「未知」，不是留一行空白"
    assert "—" in unknown[0], f"修复数是 NULL，必须显示破折号：{unknown[0]}"
    assert "修复 0" not in out, "NULL 渲染成 0 = 替库里没有的数据下结论"
    # 对照组：解得出来的那一批必须真的显示数字，否则上面在「所有行都是 —」
    # 的实现下也是绿的
    # 按行首取，不用「含 pytest」：抬头那行印的是 db 路径，而临时目录
    # 恰好叫 pytest-of-<user>，含子串的取法会取到抬头
    pytest_line = [ln for ln in out.splitlines() if ln.startswith("  pytest：")]
    assert pytest_line and "修复 1" in pytest_line[0], pytest_line
