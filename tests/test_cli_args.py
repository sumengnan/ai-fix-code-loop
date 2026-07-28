import pytest

from aifix.cli import build_parser


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
