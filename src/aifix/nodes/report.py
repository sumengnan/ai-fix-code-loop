from __future__ import annotations

from pathlib import Path
from typing import Any

from ..budget import fmt_usd

_VERDICT_CN = {"better": "已修复", "same": "未改善", "worse": "引入回归"}


def render_report(state: dict[str, Any]) -> str:
    abort = state.get("abort")
    results = state["results"]
    # 中止发生在 baseline 之前（preflight 不通过）时确实无事可报；但预算耗尽、
    # 熔断这类中止发生在**已有成果之后** —— 早返回会把已经修好并提交到交付
    # 分支的用例整个吞掉，用户只看到「钱花完了」，不知道分支上躺着可合并的修复。
    if abort and not results and not state["baseline_ids"]:
        return (f"# aifix run {state['run_id']}\n\n"
                f"**中止**：{abort}\n")

    fixed = sum(1 for r in results if r["verdict"] == "better")
    total = len(state["baseline_ids"])
    tokens = state["spent_tokens"]
    usd = state["spent_usd"]
    # 花了 token 却算出 0 元，说明没配价格表。显示假的 $0.00 比不显示更糟。
    cost = (f"未知（未配置 AIFIX_PRICE_MAP）（{tokens:,} tokens）"
            if tokens > 0 and usd == 0.0
            else f"{fmt_usd(usd)}（{tokens:,} tokens）")
    lines = [
        f"# aifix run {state['run_id']}",
        "",
    ]
    if abort:
        lines += [f"> **中止**：{abort}", ""]
    lines += [
        f"- 适配器：{state['adapter_name']}",
        f"- 分支：`{state['branch']}`",
        f"- 修复：**{fixed} / {total}**",
        f"- 成本：{cost}",
        "",
        "| 测试用例 | 结果 | 尝试次数 | 中止原因 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r['test_id']}` | {_VERDICT_CN.get(r['verdict'], r['verdict'])} "
            f"| {r['attempts']} | {r['abort_reason'] or '—'} |")
    lines += ["", f"合并：`git merge {state['branch']}`"]
    return "\n".join(lines) + "\n"


def report_node(state: dict[str, Any]) -> dict[str, Any]:
    """渲染报告；有产物目录就一并落盘，和 facts / events 放在一起。"""
    md = render_report(state)
    out = state.get("artifact_dir")
    if out:
        p = Path(out)
        p.mkdir(parents=True, exist_ok=True)
        (p / "report.md").write_text(md, encoding="utf-8")
    return {"report_md": md}
