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


_TEST_ONLY_IDENTITY = '''from calc import add


def test_identity():
    assert add(0, 0) == 0
'''

_TEST_BOTH = '''from calc import add


def test_identity():
    assert add(0, 0) == 0


def test_add():
    assert add(2, 3) == 5
'''


@pytest.fixture
def history_repo(tmp_path: Path) -> dict:
    """一个带「红转绿」commit 的仓库，供挖掘与评测使用。

    C^ : calc.py 有 bug，测试里只有 test_identity（通过）
    C  : calc.py 修好，测试里多出 test_add

    于是「C^ 的源码 + C 的测试」= test_add 红，而 C 处全绿 ——
    正是 aifix mine 要找的形状。
    """
    repo = tmp_path / "hist"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        _TEST_ONLY_IDENTITY, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "calc.py").write_text(_FIXED, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST_BOTH, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: add 应为加法")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    return {"path": repo, "base": base, "commit": commit,
            "test_files": ["tests/test_calc.py"], "gold_files": ["calc.py"],
            "target": "tests/test_calc.py::test_add"}
