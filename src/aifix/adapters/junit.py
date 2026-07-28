"""JUnit XML → FailureSet。pytest / Maven Surefire / Gradle / Jest 共享此解析。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

from .base import Failure, FailureSet

MakeTestId = Callable[[str, str, str | None], str]


def parse_junit(paths: Iterable[Path], make_test_id: MakeTestId) -> FailureSet:
    """解析一批 JUnit XML，收集所有 <failure> 与 <error> 的用例。

    make_test_id 由适配器提供：报告里的 classname 未必能直接拿去重跑
    （pytest 给的是点分模块名，重跑要的是文件路径形式）。
    """
    failures: dict[str, Failure] = {}
    ran: set[str] = set()
    for path in paths:
        if not Path(path).is_file():
            # 报告缺失（如测试进程崩溃）不算解析错误。调用方若需要区分
            # 「跑完了、全绿」与「压根没跑成」，用 run_full_suite 的
            # require_report=True。
            continue
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            file = case.get("file")
            raw_line = case.get("line")
            test_id = make_test_id(classname, name, file)
            # 注意：Element 无子元素时为 falsy，必须显式与 None 比较
            if case.find("skipped") is None:
                ran.add(test_id)
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if bad is None:
                continue
            failures[test_id] = Failure(
                test_id=test_id,
                classname=classname,
                name=name,
                message=bad.get("message", ""),
                trace=(bad.text or ""),
                file=file,
                line=int(raw_line) if raw_line and raw_line.isdigit() else None,
            )
    return FailureSet(failures, ran=frozenset(ran))
