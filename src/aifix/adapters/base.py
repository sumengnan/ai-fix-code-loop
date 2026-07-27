from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Failure:
    """一个失败的测试用例。test_id 必须可直接喂回 run_tests。"""
    test_id: str
    classname: str
    name: str
    message: str
    trace: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class SourceCandidate:
    """从栈帧还原出的嫌疑源码位置，按可疑度排序（越靠前越可疑）。"""
    path: str
    line: int
    frame: str


@dataclass(frozen=True)
class FailureSet:
    failures: dict[str, Failure]

    @property
    def ids(self) -> set[str]:
        return set(self.failures)


class Verdict(str, Enum):
    BETTER = "better"
    SAME = "same"
    WORSE = "worse"


class ProjectAdapter(Protocol):
    """把「某种语言的测试工程」翻译成核心循环认识的四个问题 + 一个真活。"""

    name: str

    @staticmethod
    def detect(repo: Path) -> bool: ...

    def full_test_command(self, report_path: str) -> list[str]: ...

    def scoped_test_command(self, test_ids: list[str], report_path: str) -> list[str]: ...

    def report_glob(self) -> str: ...

    def test_dirs(self) -> list[str]: ...

    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
