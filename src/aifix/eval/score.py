"""双档打分与跨模型对比表（规格 §9）。

- 定位准确率 = locate_hit 占比 → Detector 的能力
- 修复成功率 = verdict == better 占比 → 整体能力

两档分开：定位错了但改对了是常见情形（模型自己读代码纠正了诊断），
合成一个数字就看不见 Detector 到底有没有用。
"""
from __future__ import annotations

from pydantic import BaseModel

from .task import TaskResult


class Summary(BaseModel):
    model: str
    tasks: int                  # 有效任务数（不含出错的）
    locate_rate: float
    fix_rate: float
    avg_cost_usd: float
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
                       avg_cost_usd=0.0, avg_attempts=0.0,
                       violations=sum(r.violations for r in results),
                       errors=len(results) - n)
    return Summary(
        model=model, tasks=n,
        locate_rate=sum(r.locate_hit for r in valid) / n,
        fix_rate=sum(r.verdict == "better" for r in valid) / n,
        avg_cost_usd=sum(r.cost_usd for r in valid) / n,
        avg_attempts=sum(r.attempts for r in valid) / n,
        violations=sum(r.violations for r in valid),
        errors=len(results) - n,
    )


def render_table(summaries: list[Summary]) -> str:
    lines = [
        "| 模型 | 任务数 | 定位准确率 | 修复成功率 | 平均成本 | 平均尝试 |"
        " 越界尝试 | 评测故障 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.model} | {s.tasks} | {s.locate_rate:.0%} | {s.fix_rate:.0%}"
            f" | ${s.avg_cost_usd:.3f} | {s.avg_attempts:.1f}"
            f" | {s.violations} | {s.errors} |")
    return "\n".join(lines) + "\n"
