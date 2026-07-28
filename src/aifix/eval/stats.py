"""区间估计。样本量少的时候，一个百分比不是结论。"""
from __future__ import annotations

# 95% 双侧
Z95 = 1.959963984540054


def wilson(hits: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval。返回 (下界, 上界)，都在 [0, 1]。

    为什么不用正态近似（Wald，p̂ ± z·√(p̂(1-p̂)/n)）：p̂ 取 0 或 1 时方差算出
    0，区间塌成一个点 —— 1/1 会得到 [100%, 100%]，一个宣称"确定无疑"的
    区间。而"只跑了一个任务"恰恰是这张对比表最需要说出口的事。Wilson 在
    同样输入下给出 [21%, 100%]，一眼看出没有结论。

    n = 0 时返回 (0, 0)：没有样本就没有区间，调用方据此渲染成「—」。
    """
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(center - half, 0.0), min(center + half, 1.0))
