from __future__ import annotations

from pathlib import Path
from typing import Any

from ..delivery import ensure_clean
from ..graph import AifixState
from .baseline import detect_adapter


def preflight_node(state: AifixState) -> dict[str, Any]:
    """探测适配器 + 确认主工作区干净。任一不满足即中止整个 run。

    探测本身在 baseline.detect_adapter 里，只有那一份：此处曾另存一份只含
    PytestAdapter 的列表，于是 adapter_name 的真正来源和登记新适配器的地方
    不是同一处；`aifix mine` 后来也各写了一份（写死 PytestAdapter()）。
    """
    repo = Path(state["repo"])
    adapter = detect_adapter(repo)
    if adapter is None:
        return {"abort": f"没有适配器认领这个项目：{repo}"}
    try:
        ensure_clean(repo)
    except RuntimeError as e:
        return {"abort": str(e)}
    return {"adapter_name": adapter.name, "abort": None}
