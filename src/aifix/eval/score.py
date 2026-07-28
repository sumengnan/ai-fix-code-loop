"""双档打分与跨模型对比表（规格 §9）。

- 定位准确率 = locate_hit 占比 → Detector 的能力
- 修复成功率 = verdict == better 占比 → 整体能力

两档分开：定位错了但改对了是常见情形（模型自己读代码纠正了诊断），
合成一个数字就看不见 Detector 到底有没有用。
"""
from __future__ import annotations

from pydantic import BaseModel

from ..budget import fmt_usd
from .task import TaskResult


class Summary(BaseModel):
    model: str
    tasks: int                  # 有效任务数（不含出错的）
    locate_rate: float
    fix_rate: float
    avg_cost_usd: float
    # 用来判断「到底花没花 token」：没配价格表时 cost_usd 恒为 0，
    # 光看成本分不出「便宜」和「没数据」。
    avg_tokens: float
    avg_attempts: float
    violations: int             # 总次数，不是均值
    errors: int


def summarize(results: list[TaskResult]) -> Summary:
    model = results[0].model if results else ""
    # 出错的任务不进分母：那是评测自己的故障，不该拉低被测系统的成绩
    valid = [r for r in results if r.error is None]
    n = len(valid)
    if n == 0:
        return Summary(model=model, tasks=0, locate_rate=0.0, fix_rate=0.0,
                       avg_cost_usd=0.0, avg_tokens=0.0, avg_attempts=0.0,
                       violations=sum(r.violations for r in results),
                       errors=len(results) - n)
    return Summary(
        model=model, tasks=n,
        locate_rate=sum(r.locate_hit for r in valid) / n,
        fix_rate=sum(r.verdict == "better" for r in valid) / n,
        avg_cost_usd=sum(r.cost_usd for r in valid) / n,
        avg_tokens=sum(r.tokens for r in valid) / n,
        avg_attempts=sum(r.attempts for r in valid) / n,
        violations=sum(r.violations for r in valid),
        errors=len(results) - n,
    )


def _cost_cell(s: Summary) -> str:
    """花了 token 却算出 0 元，说明没配价格表 —— 显示假的 $0.000 比不显示更糟。

    跨模型对比表就是拿来决定「哪个模型更划算」的：一列整齐的 $0.000 会被
    读成「极其便宜」，而不是「这一列没数据」。report.py 已经这么处理，
    config.price_map 的注释也做了同样的承诺。
    """
    if s.avg_tokens > 0 and s.avg_cost_usd == 0.0:
        return "未知（未配置 AIFIX_PRICE_MAP）"
    return fmt_usd(s.avg_cost_usd)


def render_table(summaries: list[Summary]) -> str:
    lines = [
        "| 模型 | 任务数 | 定位准确率 | 修复成功率 | 平均成本 | 平均尝试 |"
        " 越界尝试 | 评测故障 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.model} | {s.tasks} | {s.locate_rate:.0%} | {s.fix_rate:.0%}"
            f" | {_cost_cell(s)} | {s.avg_attempts:.1f}"
            f" | {s.violations} | {s.errors} |")
    return "\n".join(lines) + "\n"
