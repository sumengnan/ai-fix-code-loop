"""JUnit XML → FailureSet。pytest / Maven Surefire / Gradle / Jest 共享此解析。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

from .base import Failure, FailureSet

MakeTestId = Callable[[str, str, str | None], str]

# 终端色码。**两种形态都要认**，而字面量那种是真正会遇到的：
#
# XML 1.0 不允许控制字符，pytest 的 junitxml 于是把 `\x1b` 转义成**七个可见
# 字符** `#x1B`（`bin_xml_escape`，`"#x%02X" % ord(c)`）。也就是说报告里躺着的
# 是字面文本 `#x1B[31m`，不是转义序列 —— 任何匹配 `\x1b` 的「去 ANSI」正则对它
# 完全无效，而它看起来又像是已经处理过了。
#
# 不去掉的后果是**静默的定位失效**：`_PYTEST_FRAME` 会把
# `#x1B[1m#x1B[31mcalc.py#x1B[0m:5: NameError` 的路径截成
# `#x1B[1m#x1B[31mcalc.py#x1B[0m`，`_resolve` 拿它做 is_file() 落空，于是整条
# traceback **一帧都定位不到**，Detector 收到「未能从栈帧定位到 repo 内的源码」
# 然后盲猜路径。实测（2026-08-03）同一份失败的两份报告：有色 locate_source 返回
# `[]`，无色返回 `[('calc.py', 5, 'traceback'), ...]`。
#
# 什么时候会有色：pytest 认 `FORCE_COLOR`，与是不是 tty 无关。GitHub Actions
# 上没有这个变量，所以 CI 一直是无色的 —— 这条差异正是它至今没被发现的原因，
# 而**本地跑 aifix** 恰恰是最容易踩到的场景。
#
# 顺带省掉一笔 token：trace 会原样进 Detector 与 Fixer 的 prompt
# （detector.py / fixer.py），色码在那里是纯噪声。
_ANSI = re.compile(r"(?:\x1b|#x1[bB])\[[0-9;]*m")


def _decolor(text: str) -> str:
    return _ANSI.sub("", text)


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
                message=_decolor(bad.get("message", "")),
                trace=_decolor(bad.text or ""),
                file=file,
                line=int(raw_line) if raw_line and raw_line.isdigit() else None,
            )
    return FailureSet(failures, ran=frozenset(ran))
