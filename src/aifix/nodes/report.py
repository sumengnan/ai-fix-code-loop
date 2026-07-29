from __future__ import annotations

from pathlib import Path
from typing import Any

from ..budget import fmt_usd

_VERDICT_CN = {"better": "已修复", "same": "未改善", "worse": "引入回归"}

_SIGNAL_CN = [
    ("removed_public_symbols", "补丁删除了公开符号"),
    ("new_module_state", "补丁新增了模块级可变状态"),
    ("files_outside_suspect", "改动落在诊断的嫌疑文件之外"),
]


def _signal_section(signals: list[dict[str, Any]]) -> list[str]:
    """静态信号一节：只在真有信号时出现，且**按 test_id 分组**。

    恒定出现的一节会被人当成模板噪音无视掉，而它存在的全部意义就是在少数几
    次里被看见。

    分组不是排版偏好：一个 run 会依次修好多个 failure，把所有补丁的信号合成
    一份并集，人就分不清「删掉的那个公开符号」是修哪一个用例时删的 ——
    要去看的 diff 是哪一个 commit 也就无从谈起。
    """
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for entry in signals:
        # 形状检查：`state["signals"]` 从 dict 换成 list 之后，旧 checkpoint
        # 里存的还是 dict，`list(那个 dict)` 得到的是一串字符串键 ——
        # `entry.get` 当场 AttributeError，而此时修复早已提交进交付分支，
        # 用户拿到的是一个「全都做完了却在最后一步炸掉」的 run。
        if not isinstance(entry, dict):
            continue
        if not any(entry.get(k) for k, _ in _SIGNAL_CN):
            continue
        test_id = entry.get("test_id") or "—"
        if groups and groups[-1][0] == test_id:
            groups[-1][1].append(entry)
        else:
            groups.append((test_id, [entry]))
    if not groups:
        return []

    lines = ["", "## ⚠️ 值得多看一眼", ""]
    for test_id, entries in groups:
        lines += [f"修复 `{test_id}` 的补丁：", ""]
        for entry in entries:
            for key, label in _SIGNAL_CN:
                if entry.get(key):
                    lines.append(
                        f"- {label}："
                        f"{'、'.join('`%s`' % x for x in entry[key])}")
        lines.append("")
    return lines + ["这些是静态信号，**不改变判定** —— 测试确实转绿了。"
                    "它们只是说：合并之前值得亲眼看一遍这个 diff。"]


def count_fixed(results: list[dict[str, Any]]) -> int:
    """判定为「已修复」的用例数。

    报告里的那个数与落进 trajectory 的 fixed 列必须是同一个 —— 各算各的，
    两边的口径迟早会分家，而分家之后两个数都还是「看着对」。
    """
    return sum(1 for r in results if r["verdict"] == "better")


def cost_is_unknown(tokens: int, usd: float) -> bool:
    """花了 token 却算出 0 元 —— 没配价格表，effective_cost 恒为 0。

    这个 0 与「真的没花钱」在这里区分不了，所以一律当作「不知道」：
    显示假的 $0.00、往库里存一个 0.0，都会让此后按成本做的排序与汇总变成
    看起来完全正常的假结论。
    """
    return tokens > 0 and usd == 0.0


def render_report(state: dict[str, Any]) -> str:
    abort = state.get("abort")
    results = state["results"]
    # 中止发生在 baseline 之前（preflight 不通过）时确实无事可报；但预算耗尽、
    # 熔断这类中止发生在**已有成果之后** —— 早返回会把已经修好并提交到交付
    # 分支的用例整个吞掉，用户只看到「钱花完了」，不知道分支上躺着可合并的修复。
    if abort and not results and not state["baseline_ids"]:
        return (f"# aifix run {state['run_id']}\n\n"
                f"**中止**：{abort}\n")

    fixed = count_fixed(results)
    total = len(state["baseline_ids"])
    tokens = state["spent_tokens"]
    usd = state["spent_usd"]
    # 显示假的 $0.00 比不显示更糟，见 cost_is_unknown
    cost = (f"未知（未配置 AIFIX_PRICE_MAP）（{tokens:,} tokens）"
            if cost_is_unknown(tokens, usd)
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
    lines += _signal_section(state.get("signals") or [])

    # 一个都没修好时不给合并命令：那条分支与 HEAD 逐字相同，`git merge` 是在
    # 邀请用户去合一个空分支。fixed > 0 恰好就是「分支上至少多了一个提交」——
    # results 里的 better 行只在 Worktree.commit 真的产生了提交之后才写
    # （见 verify_node），两者不是各算各的。
    if fixed:
        lines += ["", f"合并：`git merge {state['branch']}`"]
    else:
        lines += ["", f"这条分支上没有任何提交（`{state['branch']}` 与 HEAD "
                      "相同），没有可合并的东西。"]
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
