from __future__ import annotations

from pathlib import Path
from typing import Any

from ..runtime.money import fmt_cny
from ..graph import COLLECTION_ABORT_KIND

_VERDICT_CN = {"better": "已修复", "same": "未改善", "worse": "引入回归"}

_SIGNAL_CN = [
    ("removed_public_symbols", "补丁删除了公开符号"),
    ("new_module_state", "补丁新增了模块级可变状态"),
    ("files_outside_suspect", "改动落在诊断的嫌疑文件之外"),
    ("hardcoded_literals", "新增的判断用到了目标测试里的字面量"),
    # 这一条的值不是字符串列表而是 {label, preview} 的列表，由
    # `_unnecessary_lines` 单独渲染 —— 见那里。
    ("unnecessary_hunks", "撤掉之后目标用例照样绿（对修复没有贡献）"),
    # 反查自己的覆盖面，不是补丁的毛病。列出来是因为**没有结论**和「查过、
    # 是必要的」在结果里长得一样，不点名的话上面那份名单看起来就是完整的。
    ("necessity_skipped", "反查没能把它单独撤下来，对它没有结论"),
    # 只有在 attempt 用尽、补丁仍被交付时才会出现（还有额度就退回重写了）。
    ("metamorphic_diverged", "把测试里的字面量换个序，这个补丁就不成立了"),
]

# 变形复跑那一条的注脚。它比前面几类都硬：报出来之前跑过对照组（同一个变形
# 在未打补丁的代码上仍然红），所以不是「可能有问题」而是「这个形状确实没修」。
_METAMORPHIC_NOTE = (
    "变形那一条**带对照组**：同一个变形在未打补丁的代码上仍然红，"
    "说明它依然测得到那个缺陷 —— 所以补丁在这个形状下确实没修好。")

# 必要性反查那一条的注脚。单独一条是因为它的判据和前三条不是一回事：前三条
# 是纯 AST 的**静态**信号，这一条要真跑测试，且**只跑目标那一条用例** ——
# 于是「为了不打破别的用例而改的调用点」会被报出来，而它其实是必要的。
# 不写清楚的话，人会按「多余 = 可以删」去读它，那是这一节最贵的误读。
_NECESSITY_NOTE = (
    "「撤掉之后目标用例照样绿」这一条只按**目标用例**判，没有跑全量 —— "
    "为了不打破**别的**用例而做的改动会出现在这里，那是误报。")

# 反查覆盖不全时的注脚。必须和上面那条分开：上面说的是「报出来的可能是错
# 的」，这一条说的是「**没报出来的**不代表查过」——两句话的方向相反，合成
# 一段会互相稀释。
_NECESSITY_PARTIAL_NOTE = (
    "反查这一层**没有查完**：没被报出来不等于都必要。")

# 裁判那一条的注脚。它和上面两条不是一个性质：前面几类的判据是确定性的（AST、
# 路径、真跑一遍测试），这一条是一个模型的看法 —— 会看错，也不可复现。写出来
# 是因为「可疑」两个字读起来的分量和「删除了公开符号」一样重，而它们的证据强度
# 差着一整档。
_REVIEWER_NOTE = (
    "裁判那一条是**一个模型的看法**，不是确定性判据 —— 它会看错，"
    "同一个补丁两次跑也可能给不同的判断。它同样**不改变判定**。")


def _unnecessary_lines(entry: dict[str, Any], label: str) -> list[str]:
    """必要性反查那一条：定位 + 那几行改了什么。

    和别的几类不同 —— 它们的值是一串名字（符号名、路径），一行列完就够了；
    这一条的值是**代码位置**，只给 `calc.py:10-13` 的话，人拿到报告的下一步
    必然是打开 diff 去数行。把那几行贴出来，是为了让「值不值得细看」这个判断
    在报告里就能做完。

    元素既接受 `{label, preview}` 也接受裸字符串：`state["signals"]` 会从旧
    checkpoint 里恢复，而这个 key 早先存的就是一串标签。少这一层兼容，
    读一个旧 run 的报告会在 `entry["label"]` 上当场 AttributeError —— 而那时
    修复早已提交进交付分支。
    """
    items = entry.get("unnecessary_hunks") or []
    lines = [f"- {label}："]
    for item in items:
        if isinstance(item, str):
            lines.append(f"  - `{item}`")
            continue
        if not isinstance(item, dict):
            continue
        lines.append(f"  - `{item.get('label', '—')}`")
        preview = item.get("preview") or ""
        if preview:
            # 缩进 4 格让代码块留在上一级列表项里；标 diff 让 +/- 有颜色
            lines.append("    ```diff")
            lines += [f"    {ln}" for ln in preview.splitlines()]
            lines.append("    ```")
    return lines


def _reviewer_line(entry: dict[str, Any]) -> str | None:
    """裁判模型那一条。

    单独一行而不是塞进 `_SIGNAL_CN`：它的值是一句话不是一串名字。而且它得带上
    出处 —— 前面几类都是确定性判据，这一条是**一个模型的看法**，人读到「可疑」
    两个字时必须知道说这话的是谁，否则会把它当成和「删除了公开符号」同等强度
    的事实。
    """
    note = entry.get("reviewer_note")
    return f"- 裁判模型认为这个补丁可疑：{note}" if note else None


def _over_cap_line(entry: dict[str, Any]) -> str | None:
    """补丁大到整层没跑时的那一行。

    单独一行而不是塞进 _SIGNAL_CN：它的值是个数字不是列表，而且它说的是
    「这一层根本没开口」，与「这一层报出了什么」不是一类信息。
    """
    n = entry.get("necessity_over_cap")
    return (f"- 补丁拆出 {n} 处改动，超过反查上限，**必要性反查整层没有跑**"
            if isinstance(n, int) and n > 0 else None)


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
        if (not any(entry.get(k) for k, _ in _SIGNAL_CN)
                and not _over_cap_line(entry)
                and not _reviewer_line(entry)):
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
                if not entry.get(key):
                    continue
                if key == "unnecessary_hunks":
                    lines += _unnecessary_lines(entry, label)
                else:
                    lines.append(
                        f"- {label}："
                        f"{'、'.join('`%s`' % x for x in entry[key])}")
            over_cap = _over_cap_line(entry)
            if over_cap:
                lines.append(over_cap)
            reviewer = _reviewer_line(entry)
            if reviewer:
                lines.append(reviewer)
        lines.append("")
    tail = ["这些是信号，**不改变判定** —— 测试确实转绿了。"
            "它们只是说：合并之前值得亲眼看一遍这个 diff。"]
    # 两条注脚都只在对应的那一类真出现时才加：恒定出现的免责声明会连着上面
    # 那几条一起被当成模板噪音跳过。
    entries_all = [e for _, es in groups for e in es]
    if any(e.get("unnecessary_hunks") for e in entries_all):
        tail += ["", _NECESSITY_NOTE]
    if any(e.get("necessity_skipped") or _over_cap_line(e)
           for e in entries_all):
        tail += ["", _NECESSITY_PARTIAL_NOTE]
    if any(e.get("metamorphic_diverged") for e in entries_all):
        tail += ["", _METAMORPHIC_NOTE]
    if any(e.get("reviewer_note") for e in entries_all):
        tail += ["", _REVIEWER_NOTE]
    return lines + tail


def _invariant_section(invariant: str | None) -> list[str]:
    """复现那一步说的「这条测试钉的是什么规则」。没有就一行都不加。

    要写出来，是因为判定只看那一条测试，而它只有一个样本点：补丁扛过了那个
    样本不等于修好了。最后那道闸是人，人要判断「这个 diff 是不是按规则改的」
    就得看见规则。

    措辞必须标明出处 —— 它是模型写的一句话，排版上和「适配器」「分支」一样
    会被当成事实。
    """
    if not invariant:
        return []
    return ["", "## 这条复现测试钉的规则", "",
            f"> {invariant}", "",
            "写复现的那一步给的说法，**仅供参考**，不参与判定 —— "
            "判定只看测试结果。合并之前值得对着它看一眼 diff："
            "补丁是按这条规则改的，还是只让那一条用例通过。"]


def _untouching_section(untouching: bool) -> list[str]:
    """复现测试没 import 本项目任何模块，而且退回重写一次之后仍然如此。

    这一节比其余信号都硬：它说的不是「补丁可能有问题」，而是**判定所依据的
    那条测试本身可能测不到行为**。一条把源文件当文本读一遍再 grep 的测试，
    在修复前后都只反映文件内容 —— 它红了又绿了，却区分不出「实现了」和
    「明确没实现」（ai-learning-helper#95 的形状）。

    仍然交付而不是判失败，是因为判据是启发式的：只用 subprocess 跑 CLI 的
    合法测试同样不 import 本项目。误报的成本由「只退一次」封顶，代价就是这
    一节必须**足够刺眼**，否则放行等于静默。
    """
    if not untouching:
        return []
    return ["", "## ⚠ 这条复现测试可能没有真的执行被测代码", "",
            "它没有 import 本项目的任何模块，退回重写一次之后仍然如此。",
            "",
            "判定只看测试结果，而这条测试可能只反映文件内容、测不到行为 —— "
            "**合并之前请亲自确认它真的钉住了那个缺陷**：把补丁撤掉，"
            "它应该变红。",
            "",
            "（判据是启发式的：只用 subprocess 跑 CLI 的合法测试也会命中这一条。）"]


def count_fixed(results: list[dict[str, Any]]) -> int:
    """判定为「已修复」的用例数。

    报告里的那个数与落进 trajectory 的 fixed 列必须是同一个 —— 各算各的，
    两边的口径迟早会分家，而分家之后两个数都还是「看着对」。
    """
    return sum(1 for r in results if r["verdict"] == "better")


def cost_is_unknown(tokens: int, cny: float) -> bool:
    """花了 token 却算出 0 元 —— 没配价格表，effective_cost 恒为 0。

    这个 0 与「真的没花钱」在这里区分不了，所以一律当作「不知道」：
    显示假的 ¥0.00、往库里存一个 0.0，都会让此后按成本做的排序与汇总变成
    看起来完全正常的假结论。
    """
    return tokens > 0 and cny == 0.0


def _ask_section(ask: dict[str, Any] | None, run_id: str) -> list[str]:
    """待人回答的那个问题。没有就一行都不加。

    必须把**怎么回答**写全（连命令一起给出来）：这是报告里唯一要求读者动手的
    地方，而一个「我需要更多信息」却不说怎么给的提示，等于把这次 run 变成一
    条死路。
    """
    if not ask:
        return []
    out = ["## 需要你回答一个问题", "",
           f"卡在 `{ask.get('test_id', '')}` 上：", "",
           f"**{ask.get('question', '')}**", ""]
    for i, opt in enumerate(ask.get("options") or [], 1):
        out.append(f"{i}. {opt}")
    out += ["",
            f"回答：`aifix answer <编号>`（如 `aifix answer 1`），"
            f"或在 issue 下回复 `/aifix <编号>`。",
            "",
            "答复之后会**重新跑一遍**（不是从断点继续），所以那次会再花一次"
            "baseline 的时间。",
            ""]
    return out


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
    cny = state["spent_cny"]
    # 显示假的 ¥0.00 比不显示更糟，见 cost_is_unknown
    # 汇率要跟着金额一起印：一个不写汇率的人民币金额会被当成实时汇率折的，
    # 而它是个写死的约数（见 money.DEFAULT_USD_TO_CNY）。
    # 防御性地取：报告是用户手里唯一的成果凭据，渲染这一步不该因为一个
    # 缺失的键而炸掉整次 run。取不到就不印汇率 —— 印一个默认汇率是在编。
    cfg = state.get("config")
    note = cfg.money.rate_note() if cfg is not None else ""
    cost = (f"未知（未配置 AIFIX_PRICE_MAP）（{tokens:,} tokens）"
            if cost_is_unknown(tokens, cny)
            else f"{fmt_cny(cny)}（{tokens:,} tokens"
                 f"{'，' + note if note else ''}）")
    lines = [
        f"# aifix run {state['run_id']}",
        "",
    ]
    if abort:
        lines += [f"> **中止**：{abort}", ""]
    # 待答的问题排在**最前面**、在成绩单之前：这次 run 的产出就是这个问题，
    # 把它塞在表格底下等于让人自己去找。这也是报告里唯一一处要求读者动手的
    # 地方 —— 别的部分都是「已经发生了什么」。
    lines += _ask_section(state.get("ask"), state.get("run_id", ""))
    # 「修复 x / y」在收集错误中止下长得和一个成绩一模一样，而这次根本没开修：
    # 分母是一批本就不该存在的工单数（每一条都是「某个测试文件没能导入」），
    # 分子是「一个都没轮到」。用户实测里看到的正是这一行 ——「修复 0 / 11」——
    # 它把一次环境故障读成了模型的失分。别的中止（预算耗尽、熔断）不在此列：
    # 那时 baseline 是可信的，分母有意义，已修好的那些也该被数出来。
    fixed_line = (
        "- 修复：—（baseline 不可信，一个用例都没开修，见上方中止说明）"
        if state.get("abort_kind") == COLLECTION_ABORT_KIND
        else f"- 修复：**{fixed} / {total}**")
    lines += [
        f"- 适配器：{'、'.join(state['adapter_names']) or '（无）'}",
        f"- 分支：`{state['branch']}`",
        fixed_line,
        f"- 成本：{cost}",
        "",
        "| 测试用例 | 结果 | 尝试次数 | 中止原因 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r['test_id']}` | {_VERDICT_CN.get(r['verdict'], r['verdict'])} "
            f"| {r['attempts']} | {r['abort_reason'] or '—'} |")
    lines += _untouching_section(bool(state.get("repro_untouching")))
    lines += _invariant_section(state.get("invariant"))
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
