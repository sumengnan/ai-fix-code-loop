from __future__ import annotations

import re
import sys
from pathlib import Path

from .base import Failure, SourceCandidate

# 形如：  File "/abs/path/calc.py", line 2, in add
_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')

_MARKERS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py")


class PytestAdapter:
    name = "pytest"

    @staticmethod
    def detect(repo: Path) -> bool:
        if any((repo / m).is_file() for m in _MARKERS):
            return True
        return (repo / "tests").is_dir()

    def full_test_command(self, report_path: str) -> list[str]:
        return [sys.executable, "-m", "pytest", "-q",
                f"--junitxml={report_path}", "-p", "no:cacheprovider"]

    def scoped_test_command(self, test_ids: list[str], report_path: str) -> list[str]:
        return [sys.executable, "-m", "pytest", "-q",
                f"--junitxml={report_path}", "-p", "no:cacheprovider", *test_ids]

    def report_glob(self) -> str:
        return ".aifix-report.xml"

    def test_dirs(self) -> list[str]:
        return ["tests", "test"]

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """pytest 重跑要的是 `路径::用例`，而报告给的 classname 是点分模块名。"""
        path = file or (classname.replace(".", "/") + ".py")
        return f"{path}::{name}"

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 traceback 抽出 repo 内部帧，最深的排最前。"""
        repo_real = str(Path(repo).resolve())
        out: list[SourceCandidate] = []
        for m in _FRAME.finditer(failure.trace):
            raw = m.group("path")
            try:
                real = str(Path(raw).resolve())
            except OSError:
                continue
            if not (real == repo_real or real.startswith(repo_real + "/")):
                continue
            if "site-packages" in real or "/dist-packages/" in real:
                continue
            out.append(SourceCandidate(
                path=str(Path(real).relative_to(repo_real)),
                line=int(m.group("line")),
                frame=m.group("fn"),
            ))
        out.reverse()          # traceback 由浅入深，最深的最可疑
        return out
