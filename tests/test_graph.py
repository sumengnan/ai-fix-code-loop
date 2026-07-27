from aifix.graph import route_after_baseline, route_after_verify
from aifix.nodes.report import render_report


def test_route_after_baseline_ends_when_green():
    assert route_after_baseline({"queue": [], "abort": None}) == "report"


def test_route_after_baseline_continues_when_failures():
    assert route_after_baseline({"queue": ["a"], "abort": None}) == "detect"


def test_route_after_baseline_aborts():
    assert route_after_baseline({"queue": ["a"], "abort": "坏了"}) == "report"


def test_route_after_verify_retries_same_failure():
    assert route_after_verify({"current": "a", "queue": [], "abort": None}) == "detect"


def test_route_after_verify_takes_next():
    assert route_after_verify({"current": None, "queue": ["b"], "abort": None}) == "detect"


def test_route_after_verify_reports_when_done():
    assert route_after_verify({"current": None, "queue": [], "abort": None}) == "report"


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
