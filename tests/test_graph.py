from aifix.config import AifixConfig
from aifix.graph import route_after_baseline, route_after_verify

_CFG = AifixConfig()
from aifix.nodes.report import render_report


def test_route_after_baseline_ends_when_green():
    assert route_after_baseline({"queue": [], "abort": None}) == "report"


def test_route_after_baseline_continues_when_failures():
    assert route_after_baseline({"queue": ["a"], "abort": None}) == "detect"


def test_route_after_baseline_aborts():
    assert route_after_baseline({"queue": ["a"], "abort": "坏了"}) == "report"


def test_route_after_verify_retries_same_failure():
    assert route_after_verify(
        {"current": "a", "queue": [], "abort": None, "config": _CFG}) == "detect"


def test_route_after_verify_takes_next():
    assert route_after_verify(
        {"current": None, "queue": ["b"], "abort": None, "config": _CFG}) == "detect"


def test_route_after_verify_reports_when_done():
    assert route_after_verify(
        {"current": None, "queue": [], "abort": None, "config": _CFG}) == "report"


def test_render_report_lists_outcomes():
    md = render_report({
        "run_id": "r1", "branch": "aifix/r1", "adapter_name": "pytest",
        "baseline_ids": ["a", "b"], "spent_usd": 0.23, "spent_tokens": 12345,
        "abort": None,
        "results": [
            {"test_id": "a", "verdict": "better", "attempts": 1, "abort_reason": None},
            {"test_id": "b", "verdict": "same", "attempts": 3,
             "abort_reason": "max_attempts"},
        ],
    })
    assert "aifix/r1" in md
    assert "1 / 2" in md
    assert "$0.23" in md
    assert "max_attempts" in md


def test_render_report_shows_abort():
    md = render_report({
        "run_id": "r1", "branch": "", "adapter_name": "",
        "baseline_ids": [], "spent_usd": 0.0, "spent_tokens": 0,
        "results": [], "abort": "工作区不干净",
    })
    assert "工作区不干净" in md
    assert "修复" not in md, "preflight 阶段中止时没有任何成果可报"


def test_render_report_keeps_delivered_work_when_aborted_midrun():
    """中止不该吞掉已交付的成果。

    真实运行中预算耗尽时，已经修好并提交到交付分支的那个用例
    在报告里完全消失了 —— 用户被告知「钱花完了」，却不知道
    分支上已经躺着一个可合并的修复。
    """
    md = render_report({
        "run_id": "r1", "branch": "aifix/r1", "adapter_name": "pytest",
        "baseline_ids": ["a", "b"], "spent_usd": 0.06, "spent_tokens": 19678,
        "abort": "美元预算耗尽：$0.06 / $0.001",
        "results": [
            {"test_id": "a", "verdict": "better", "attempts": 1,
             "abort_reason": None},
        ],
    })
    assert "美元预算耗尽" in md
    assert "1 / 2" in md, "已修好的那个必须出现在报告里"
    assert "`a`" in md
    assert "git merge aifix/r1" in md, "分支上有成果就要给出合并命令"


def test_render_report_survives_a_legacy_checkpoint_signals_dict():
    """`state["signals"]` 从 dict 换成 list 之后，旧 checkpoint 还是 dict。

    `list({"removed_public_symbols": [...]})` 得到的是一串字符串键，
    `entry.get(k)` 当场 AttributeError —— 从旧 checkpoint 恢复的 run 会在
    渲染报告这一步炸掉，此时所有修复都已经提交进交付分支了。产品入口走
    run_once 不读 checkpoint，现实中打不到，但一道形状检查很便宜。
    """
    md = render_report({
        "run_id": "r1", "branch": "aifix/r1", "adapter_name": "pytest",
        "baseline_ids": ["a"], "spent_usd": 0.0, "spent_tokens": 0,
        "abort": None,
        "results": [{"test_id": "a", "verdict": "better", "attempts": 1,
                     "abort_reason": None}],
        # 旧 checkpoint 里 signals 是 dict，被 list() 一取就只剩键名
        "signals": list({"removed_public_symbols": ["mul"],
                         "new_module_state": [], "files_outside_suspect": []}),
    })
    assert "1 / 1" in md
    # 形状不对的条目跳过，而不是把键名当成信号渲染出来
    assert "值得多看一眼" not in md


def test_render_report_still_renders_well_formed_signal_entries():
    """区分度：形状检查不能顺手把正常的信号条目也跳掉。"""
    md = render_report({
        "run_id": "r1", "branch": "aifix/r1", "adapter_name": "pytest",
        "baseline_ids": ["a"], "spent_usd": 0.0, "spent_tokens": 0,
        "abort": None,
        "results": [{"test_id": "a", "verdict": "better", "attempts": 1,
                     "abort_reason": None}],
        "signals": [{"test_id": "a", "removed_public_symbols": ["mul"],
                     "new_module_state": [], "files_outside_suspect": []}],
    })
    assert "值得多看一眼" in md
    assert "`mul`" in md
