from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.pytest_adapter import PytestAdapter
from ..delivery import ensure_clean
from ..graph import AifixState

ADAPTERS = [PytestAdapter]


def preflight_node(state: AifixState) -> dict[str, Any]:
    """探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。"""
    repo = Path(state["repo"])
    for cls in ADAPTERS:
        if cls.detect(repo):
            break
    else:
        return {"abort": f"没有适配器认领这个项目：{repo}"}
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e)}
    return {"adapter_name": cls.name, "abort": None}
