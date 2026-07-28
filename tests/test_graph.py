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
