"""共享夹具：一个可复现的、带失败测试的临时 git 仓库。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b
'''

_FIXED = '''def add(a, b):
    return a + b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_identity():
    assert add(0, 0) == 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True).stdout


@pytest.fixture
def buggy_repo(tmp_path: Path) -> Path:
    """一个 pytest 项目：test_add 失败，test_identity 通过。"""
    repo = tmp_path / "proj"
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


@pytest.fixture
def fixed_source() -> str:
    return _FIXED
