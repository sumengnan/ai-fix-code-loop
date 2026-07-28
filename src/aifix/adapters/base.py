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
    # 报告里真正跑出结果的用例（通过 + 失败，不含 skipped）。
    # 「不在 failures 里」有三种可能：通过了、被删了、被跳过了。核心循环
    # 只关心第一种，所以一直不需要区分；但挖任务时把后两种当成「红转绿」
    # 会造出无人能通过的假任务，必须能分辨。默认空集 —— 手工构造
    # FailureSet 的既有调用点（verify_node）用不到这个信息。
    ran: frozenset[str] = frozenset()

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

    # 命令不接收报告路径：报告写到哪里是构建体系自己的事。Maven surefire
    # 只认 target/surefire-reports/，调用方指定不了。
    def full_test_command(self) -> list[str]: ...

    def scoped_test_command(self, test_ids: list[str]) -> list[str]: ...

    # 返回列表而非单个路径：surefire 每个测试类写一份 TEST-*.xml。
    # scoped 用于区分「全量那一跑」与「复跑那一跑」的报告 —— pytest 侧两者
    # 必须是不同文件，否则复跑会覆盖掉还要继续用的全量报告；Maven 侧同一个
    # 目录，忽略这个参数即可。
    # 一份都没写出来时返回空列表，不抛 —— 「没跑成」由 require_report 那层判定。
    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]: ...

    def test_dirs(self) -> list[str]: ...

    # 挖任务时「哪些后缀算源文件」。判据必须由适配器给，不能写死在挖掘代码
    # 里：`eval/mine.split_paths` 曾经只认 `.py`，于是 Java 仓库的源码全部
    # 落空 → gold_files 恒空 → is_candidate 恒 False → `aifix mine` 对任何
    # Maven 工程产出 0 个任务，且不报错，与「这个仓库最近没有红转绿的提交」
    # 无法区分。
    # 只收产品代码的后缀，不收资源/配置：gold_files 是 locate_hit 的判定
    # 依据，衡量的是 Detector 定位**源文件**的能力，掺进数据文件会稀释它。
    def source_suffixes(self) -> tuple[str, ...]: ...

    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
