"""把候选位置周围的**真实源码**取出来。零 LLM，纯确定性。

存在的理由：Detector 无工具、单步，在这个文件之前它看到的只有**路径和行号**。
也就是说，「这个缺陷的根本原因是什么」这个判断，是在从未见过那段代码的前提下
做出来的 —— 它能依据的只有文件名、函数名和 traceback 的措辞。`suspect_lines`
更是纯粹编的：模型没有任何办法知道第几行写着什么。

而那份诊断会原样进入 Fixer 的开场白（「嫌疑行号：120-135」）。编出来的行号
不是没用，是**有害**：它把 Fixer 的第一步引向一个具体而错误的位置。

代码就在磁盘上，读它不需要模型、不需要一个回合、不花一分钱。

给 Detector 保持「无工具、单步」不变 —— 那个设计是对的（一次调用、强制 JSON、
成本可预测）。缺的从来不是工具，是**事实**。
"""
from __future__ import annotations

from pathlib import Path

# 上下 12 行：够看清一个中等函数的主体，又不会让三个候选加起来撑爆单步调用。
_RADIUS = 12


def around(repo: Path, path: str, line: int, radius: int = _RADIUS) -> str | None:
    """`path:line` 周围的源码，带**文件里的真实行号**。读不到返回 None。

    行号必须是真实行号：模型要拿它填 `suspect_lines`，Fixer 又要拿那个去
    定位。从 1 重新编号会让整条链指向错误的位置，而且错得很安静。
    """
    root = Path(repo).resolve()
    try:
        p = (root / path).resolve()
        # 候选由适配器确定性地推出来，本来就在仓库内 —— 这道检查是防止将来
        # 有人把外部输入接到这里，而不是不信任现在的调用方。
        p.relative_to(root)
        text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None

    lines = text.splitlines()
    if not lines:
        return None
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    if start > len(lines):
        return None
    # 缺陷所在的那一行用 `>` 标出来：一屏 25 行里，模型要知道 traceback 指的
    # 到底是哪一行。不标的话它得自己数，而数数正是它最不擅长的事。
    return "\n".join(
        f"{'>' if n == line else ' '}{n:>6}\t{lines[n - 1]}"
        for n in range(start, end + 1))
