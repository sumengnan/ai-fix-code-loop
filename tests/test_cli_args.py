import re

import pytest

from aifix.cli import build_parser

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
