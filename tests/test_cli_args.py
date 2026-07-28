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
