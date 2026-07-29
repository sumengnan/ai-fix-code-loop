from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..delivery import ensure_clean
from ..graph import AifixState
from .baseline import detect_adapter


def _bad_test_python(configured: str | None) -> str | None:
    """显式配置的测试解释器不可用时返回中止理由，可用返回 None。

    只校验**显式配置**，不校验探测结果：探测本身已经要求文件存在且可执行，
    而这里要拦的是用户自己写错的那个值（路径打错、venv 删了、写成了目录）。

    必须在这里拦住而不是留给 baseline：到了那一步，exec 失败的表现是「没写出
    JUnit 报告」，用户看到的中止消息是「测试进程没能正常跑完」—— 一句指向
    目标项目的话，而真相是 aifix 的配置写错了。这个项目里最贵的失败一向不是
    崩溃，是指错方向的诊断。
    """
    if not configured:
        return None
    p = Path(os.path.expanduser(configured))
    if p.is_file() and os.access(p, os.X_OK):
        return None
    return (f"配置的测试解释器不可用：{configured}\n"
            "  AIFIX_TEST_PYTHON 要指向一个可执行的 Python 解释器"
            "（例如目标项目的 .venv/bin/python）。\n"
            "  不配这一项时 aifix 会自动探测源仓库里的 .venv / venv，"
            "都没有则退回 aifix 自己的解释器。")


def preflight_node(state: AifixState) -> dict[str, Any]:
    """校验测试解释器 + 探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。

    探测本身在 baseline.detect_adapter 里，只有那一份：此处曾另存一份只含
    PytestAdapter 的列表，于是 adapter_name 的真正来源和登记新适配器的地方
    不是同一处；`aifix mine` 后来也各写了一份（写死 PytestAdapter()）。
    """
    bad = _bad_test_python(state["config"].test_python)
    if bad:
        return {"abort": bad}
    repo = Path(state["repo"])
    adapter = detect_adapter(repo)
    if adapter is None:
        return {"abort": f"没有适配器认领这个项目：{repo}"}
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e)}
    return {"adapter_name": adapter.name, "abort": None}
