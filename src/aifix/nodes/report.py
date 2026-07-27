from __future__ import annotations

from typing import Any

_VERDICT_CN = {"better": "已修复", "same": "未改善", "worse": "引入回归"}


def render_report(state: dict[str, Any]) -> str:
    if state.get("abort"):
        return (f"# aifix run {state['run_id']}\n\n"
                f"**中止**：{state['abort']}\n")

    results = state["results"]
    fixed = sum(1 for r in results if r["verdict"] == "better")
    total = len(state["baseline_ids"])
    tokens = state["spent_tokens"]
    usd = state["spent_usd"]
    # 花了 token 却算出 0 元，说明没配价格表。显示假的 $0.00 比不显示更糟。
    cost = (f"未知（未配置 AIFIX_PRICE_MAP）（{tokens:,} tokens）"
            if tokens > 0 and usd == 0.0 else f"${usd:.2f}（{tokens:,} tokens）")
    lines = [
        f"# aifix run {state['run_id']}",
        "",
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
    return {"report_md": render_report(state)}
