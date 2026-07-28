"""双档打分与跨模型对比表（规格 §9）。

- 定位准确率 = locate_hit 占比 → Detector 的能力
- 修复成功率 = verdict == better 占比 → 整体能力

两档分开：定位错了但改对了是常见情形（模型自己读代码纠正了诊断），
合成一个数字就看不见 Detector 到底有没有用。
"""
from __future__ import annotations

from pydantic import BaseModel

from .stats import wilson
from .task import TaskResult


class Summary(BaseModel):
    model: str
    # mined | mutated | ""。空串表示「没有按来源拆」——summarize() 的老
    # 调用方（一次汇总所有结果）不关心来源，render_table 就该显示「—」
    # 而不是瞎编一个值。summarize_by_origin() 会把这里填成真实来源。
    origin: str = ""
    tasks: int                  # 有效任务数（不含出错的）
    locate_hits: int            # 分子。光有比率渲染不出 (12/20)
    fix_hits: int
    locate_rate: float
    fix_rate: float
    avg_cost_usd: float
    # 用来判断「到底花没花 token」：没配价格表时 cost_usd 恒为 0，
    # 光看成本分不出「便宜」和「没数据」。
    avg_tokens: float
    avg_attempts: float
    violations: int             # 总次数，不是均值
    errors: int


def summarize(results: list[TaskResult], origin: str = "") -> Summary:
    model = results[0].model if results else ""
    # 出错的任务不进分母：那是评测自己的故障，不该拉低被测系统的成绩
    valid = [r for r in results if r.error is None]
    n = len(valid)
    if n == 0:
        return Summary(model=model, origin=origin, tasks=0, locate_hits=0,
                       fix_hits=0, locate_rate=0.0, fix_rate=0.0,
                       avg_cost_usd=0.0, avg_tokens=0.0, avg_attempts=0.0,
                       violations=sum(r.violations for r in results),
                       errors=len(results) - n)
    locate_hits = sum(r.locate_hit for r in valid)
    fix_hits = sum(r.verdict == "better" for r in valid)
    return Summary(
        model=model, origin=origin, tasks=n,
        locate_hits=locate_hits, fix_hits=fix_hits,
        locate_rate=locate_hits / n,
        fix_rate=fix_hits / n,
        avg_cost_usd=sum(r.cost_usd for r in valid) / n,
        avg_tokens=sum(r.tokens for r in valid) / n,
        avg_attempts=sum(r.attempts for r in valid) / n,
        violations=sum(r.violations for r in valid),
        errors=len(results) - n,
    )


def summarize_by_origin(results: list[TaskResult]) -> list[Summary]:
    """按来源（mined/mutated）拆开算，不能平均成一个数字。

    挖掘任务的分布是真实 bug 的分布，人造变异任务是单点、确定、可任意
    规模但分布不同——把两者的修复成功率合并成一个数字，等于拿两个不同
    量纲的东西求平均，数字本身就是错的。

    顺序按首次出现，不按字典序：字典序会把 mutated 排到 mined 前面，
    跟任务集 jsonl 里的实际顺序对不上，读起来还得先在心里倒一遍序。
    """
    order: list[str] = []
    groups: dict[str, list[TaskResult]] = {}
    for r in results:
        if r.origin not in groups:
            order.append(r.origin)
            groups[r.origin] = []
        groups[r.origin].append(r)
    return [summarize(groups[o], origin=o) for o in order]


def _cost_cell(s: Summary) -> str:
    """花了 token 却算出 0 元，说明没配价格表 —— 显示假的 $0.0000 比不显示更糟。

    跨模型对比表就是拿来决定「哪个模型更划算」的：一列整齐的 $0.0000 会被
    读成「极其便宜」，而不是「这一列没数据」。report.py 已经这么处理，
    config.price_map 的注释也做了同样的承诺。这部分逻辑不受下面的精度
    调整影响，继续保留。
    """
    if s.avg_tokens > 0 and s.avg_cost_usd == 0.0:
        return "未知（未配置 AIFIX_PRICE_MAP）"
    # 故意不复用 budget.fmt_usd：那是给**预算总额**设计的（讲究 `--budget
    # 0.001` 不显示成 $0.00 这种大额场景），对 1~10 分区间的**单任务均价**
    # 精度不够——$0.0201 和 $0.0249 会被它渲染成同一个 $0.02，恰好抹掉跨
    # 模型成本对比最需要的那一位；它的 `%g` 分支还会给出 `$1.23e-05` 这种
    # 和同列 `$0.1234` 宽度不齐的写法。这里固定 4 位小数，两者服务的量级
    # 不同，不要为了"统一"又改回 fmt_usd。
    return f"${s.avg_cost_usd:.4f}"


def _rate_cell(hits: int, n: int) -> str:
    """比率、分数、95% 区间一起给。

    只给比率会让 1/1 的 100% 和 12/20 的 60% 长得一样重 —— M3 那张只有一个
    样本的对比表就是这么被读成结论的。n = 0 时不渲染任何数字：0% 会被读成
    「一个都没修好」，而真相是「一个有效任务都没有」。
    """
    if n <= 0:
        return "—"
    lo, hi = wilson(hits, n)
    return f"{hits / n:.0%} ({hits}/{n}, 95%CI {lo:.0%}–{hi:.0%})"


def render_table(summaries: list[Summary]) -> str:
    lines = [
        "| 模型 | 来源 | 任务数 | 定位准确率（分数, 95%CI） | 修复成功率（分数, 95%CI）"
        " | 平均成本 | 平均 tokens | 平均尝试 | 越界尝试 | 评测故障 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        # 值直接用 mined/mutated 原文：它们也是任务集 jsonl 里的字面值，
        # 翻译成中文反而和任务集文件对不上。origin 为空表示没有按来源
        # 拆（老的 summarize() 单行汇总），显示「—」而不是瞎编一个来源。
        origin_cell = s.origin if s.origin else "—"
        lines.append(
            f"| {s.model} | {origin_cell} | {s.tasks}"
            f" | {_rate_cell(s.locate_hits, s.tasks)}"
            f" | {_rate_cell(s.fix_hits, s.tasks)}"
            f" | {_cost_cell(s)} | {s.avg_tokens:,.0f} | {s.avg_attempts:.1f}"
            f" | {s.violations} | {s.errors} |")
    return "\n".join(lines) + "\n"
