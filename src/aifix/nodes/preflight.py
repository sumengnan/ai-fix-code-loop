from __future__ import annotations

from pathlib import Path
from typing import Any

from ..delivery import ensure_clean
from ..graph import AifixState
from .baseline import ADAPTERS


def preflight_node(state: AifixState) -> dict[str, Any]:
    """探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。

    注册表只有一份（baseline.ADAPTERS），这里用它的**顺序**探测、baseline
    用它的**键**取实例。此处曾另存一份只含 PytestAdapter 的列表，于是
    adapter_name 的真正来源和登记新适配器的地方不是同一处。
    """
    repo = Path(state["repo"])
    for cls in ADAPTERS.values():
        if cls.detect(repo):
            break
    else:
        return {"abort": f"没有适配器认领这个项目：{repo}"}
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e)}
    return {"adapter_name": cls.name, "abort": None}
