"""把一次 run 落下的 events.jsonl / facts.jsonl 渲染成可读的逐步复盘。

M2 起每次 run 都往 `.aifix/runs/<run_id>/` 落两份东西，但一直没有东西
消费它们 —— 出问题时只能拿眼睛读原始 jsonl。这个模块补上消费的那一侧。

输出是**一次性文本**：可 grep、可重定向、可整段贴给别人。不做交互式
TUI —— 最常见的用法是「跑一遍、翻到出问题那一步、把那几行贴出去」，
一次性输出正好满足它，交互式反而挡在中间。

## 为什么不用框架的反序列化

`harness.persistence.serialize` 只导出了 `event_to_dict`，并且它自己的
docstring 写着「单向」：没有 `event_from_dict`，也没有 `dict_to_event`。
模块里成对的只有 message / toolcall / runstate 那几组。

即便有，渲染也不需要还原成对象 —— 我们要的是「把 dict 变成人话」，
中间绕一趟 dataclass 只会多一层耦合：框架给事件加一个字段、或者哪天
换掉某个事件类的名字，还原那一步会当场抛异常，而按 `type` 分发的渲染
只会少印一行。诊断工具在数据比自己新的时候应该退化，不应该崩。

所以这里按 dict 的 `type` 字段自己分发，未知类型原样打印 data。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HR = "──"

# 没配价格表时 effective_cost 恒为 0（或 None）—— 这个项目为「假的 $0.00」
# 栽过两次（报告一次、对比表一次）。0 与「真的不花钱」在这里没法区分，
# 宁可说不知道，也不要给一个看着精确的假数字。
_COST_UNKNOWN = "未知（未配置 AIFIX_PRICE_MAP）"


def render(run_dir: Path, step: int | None = None,
           full: bool = False, max_chars: int = 2000) -> str:
    """渲染一次 run 的回放文本。

    step：只看某一步（全局步号，从 1 数起）。
    full：不截断长文本（补丁、工具返回常常几千字）。
    max_chars：单个字段的截断阈值，截断处一定留标记。
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return _missing_dir(run_dir)

    events, bad_events = _read_jsonl(run_dir / "events.jsonl")
    facts, bad_facts = _read_jsonl(run_dir / "facts.jsonl")
    steps = _split_steps(events)

    head = [f"aifix 回放 · run {run_dir.name}",
            f"运行目录：{run_dir}",
            f"事件 {len(events)} 条 · 事实 {len(facts)} 条 · 共 {len(steps)} 步"]
    for path, bad in ((run_dir / "events.jsonl", bad_events),
                      (run_dir / "facts.jsonl", bad_facts)):
        if bad:
            head.append(f"注意：{path.name} 有 {bad} 行解析不了，已跳过")
    if not (run_dir / "events.jsonl").exists():
        # 目录在、事件不在：说清楚缺的是什么，然后把还在的事实照常渲染出来
        # —— 用户要找的很可能正是那几条（比如中止原因）。
        head.append(f"缺少 events.jsonl（{run_dir} 下没有这个文件），"
                    "没有可回放的步骤；下面只有领域事实。")

    if step is not None:
        if not 1 <= step <= len(steps):
            head.append(f"没有第 {step} 步：这次 run 共 {len(steps)} 步。")
            return "\n".join(head) + "\n"
        head.append(f"（已按 step={step} 过滤；去掉这个参数可看完整时间轴与领域事实）")
        body = _render_step(step, steps[step - 1], full, max_chars)
        return "\n".join(head) + "\n\n" + body

    if events and facts:
        # 事件流里没有 failure / attempt 标记（event_to_dict 不写这两个字段，
        # RunTrace 也没往事件上贴），所以事件与事实之间**没有可靠的逐步对应
        # 关系**。硬按顺序猜能拼出一条看着精确、实则编出来的时间轴 —— 那正是
        # 这个项目一再吃亏的形状。事实的归属由它自带的 failure / attempt 写在
        # 标题里，不靠位置暗示。
        head.append("说明：事件流不带 failure / attempt 标记，"
                    "领域事实按自身归属分组列在时间轴之后。")

    parts = ["\n".join(head)]
    for i, evs in enumerate(steps, 1):
        parts.append(_render_step(i, evs, full, max_chars))
    parts.extend(_render_facts(facts, full, max_chars))
    return "\n\n".join(parts) + "\n"


# ---------- 读取 ----------

def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """读一份 jsonl，坏行只计数不抛异常 —— 半截文件也得看得到前半截。"""
    if not path.exists():
        return [], 0
    records, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        records.append(obj if isinstance(obj, dict) else {"_raw": obj})
    return records, bad


def _missing_dir(run_dir: Path) -> str:
    """找不到目录时给路，不给 traceback。"""
    lines = [f"找不到运行目录：{run_dir}",
             "  aifix 每次 run 会把轨迹写到 <repo>/.aifix/runs/<run_id>/。"]
    parent = run_dir.parent
    if parent.is_dir():
        names = sorted(p.name for p in parent.iterdir() if p.is_dir())
        if names:
            lines.append(f"  {parent} 下现有：" + "、".join(names))
        else:
            lines.append(f"  {parent} 下一个 run 都没有。")
    else:
        lines.append(f"  上级目录 {parent} 也不存在 —— 确认一下 repo 路径，"
                     "或者这个 repo 还没跑过 aifix run。")
    return "\n".join(lines) + "\n"


# ---------- 切步 ----------

def _split_steps(events: list[dict]) -> list[list[dict]]:
    """把扁平的事件流切成「步」。

    一次 run 会开好几段 AgentLoop（detect 一段、fix 每一轮守卫重试各一段），
    events.jsonl 是它们首尾相接的结果，而每段内部的 step 号都从 1 重新数。
    所以这里重新按全局顺序编号：遇到 RunStarted（新一段会话）或本块内的第
    二个 StepStarted 就切开。会话开头的 RunStarted 归入它引出的那一步。
    """
    steps: list[list[dict]] = []
    cur: list[dict] = []
    has_step = False
    for ev in events:
        kind = ev.get("type")
        if (kind == "StepStarted" and has_step) or (kind == "RunStarted" and cur):
            steps.append(cur)
            cur, has_step = [], False
        cur.append(ev)
        if kind == "StepStarted":
            has_step = True
    if cur:
        steps.append(cur)
    return steps


# ---------- 渲染 ----------

def _clip(text: str, full: bool, max_chars: int) -> str:
    """截断一定留痕。悄悄截断是这个项目最忌讳的形状 —— 守卫、预算、报告
    都吃过「静默」的亏：人看到的是完整的东西，实际上少了一截。"""
    if full or max_chars <= 0 or len(text) <= max_chars:
        return text
    return (text[:max_chars]
            + f"\n…（已截断，原文共 {len(text)} 字符；full=True 可看完整内容）")


def _block(label: str, text: str, full: bool, max_chars: int) -> str:
    """`label：正文`，多行正文缩进对齐，读起来不至于串行。"""
    clipped = _clip(text, full, max_chars)
    lines = clipped.splitlines() or [""]
    out = [f"{label}：{lines[0]}"]
    out.extend("    " + ln for ln in lines[1:])
    return "\n".join(out)


def _fmt_value(value: Any) -> str:
    # 字符串裸着显示（读起来干净），其余照 JSON 原样 —— true / ["calc.py"]
    # 这些形状要保持可辨认，别被 str() 变成 True / ['calc.py']。
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _render_args(arguments: Any, full: bool, max_chars: int) -> list[str]:
    """工具参数逐键一行。

    参数几乎总是 dict，而其中最该读的那个值往往是补丁全文 —— 整个 dict
    丢给 json.dumps 会把换行压成 \\n，一段 diff 就此不可读，而复盘要看的
    正是这段 diff。所以逐键拆开、值按原样多行显示。
    """
    if isinstance(arguments, dict):
        return [_block(f"参数 {k}", v if isinstance(v, str) else _fmt_value(v),
                       full, max_chars) for k, v in arguments.items()]
    return [_block("参数", _fmt_value(arguments), full, max_chars)]


def _fmt_usage(data: dict) -> str:
    u = data.get("usage") or {}
    cost = data.get("cost_usd")
    if not cost:
        cost_text = _COST_UNKNOWN
    elif cost < 0.0001:
        # 四舍五入到 $0.0000 又是一个「看着是零、其实不是」的数字
        cost_text = "< $0.0001"
    else:
        cost_text = f"${cost:.4f}"
    parts = [f"输入 {u.get('prompt', '?')} / 输出 {u.get('completion', '?')}"
             f" / 合计 {u.get('total', '?')} token",
             f"成本：{cost_text}"]
    if data.get("model"):
        parts.append(f"模型 {data['model']}")
    if data.get("attempts", 1) and data.get("attempts", 1) > 1:
        parts.append(f"重试 {data['attempts']} 次")
    return " · ".join(parts)


def _render_step(index: int, events: list[dict], full: bool, max_chars: int) -> str:
    lines = [f"{_HR} 步骤 {index} {_HR}"]
    # 同一个 tool_call 的参数在 ToolCallRequested 与 ToolStarted 里各有一份。
    # 一个几千字的补丁印两遍只是把输出撑长，所以第二次只报名字；但万一某条
    # 路径只发 ToolStarted，参数不能就这么丢了，于是按 id 记一下印过没有。
    args_shown: set[str] = set()
    for ev in events:
        kind = ev.get("type", "?")
        data = ev.get("data") or {}
        tag = f"  [{kind}] "
        if kind == "RunStarted":
            lines.append(tag + f"会话开始（AgentLoop run_id={data.get('run_id')}）")
        elif kind == "StepStarted":
            lines.append(tag + f"本段会话第 {data.get('step')} 步开始")
        elif kind == "StepFinished":
            lines.append(tag + f"本段会话第 {data.get('step')} 步结束")
        elif kind in ("TextDelta", "ReasoningDelta"):
            label = "模型输出" if kind == "TextDelta" else "模型思考"
            lines.append(tag + _block(label, data.get("text", ""), full, max_chars))
        elif kind == "ToolCallRequested":
            for tc in data.get("tool_calls", []):
                lines.append(tag + f"请求调用工具 {tc.get('name')}（id={tc.get('id')}）")
                lines.extend("    " + b for b in
                             _render_args(tc.get("arguments"), full, max_chars))
                args_shown.add(str(tc.get("id")))
        elif kind == "ToolStarted":
            tc = data.get("tool_call") or {}
            lines.append(tag + f"开始执行工具 {tc.get('name')}（id={tc.get('id')}）")
            if str(tc.get("id")) not in args_shown:
                lines.extend("    " + b for b in
                             _render_args(tc.get("arguments"), full, max_chars))
                args_shown.add(str(tc.get("id")))
        elif kind == "ToolFinished":
            r = data.get("result") or {}
            mark = "失败" if r.get("is_error") else "成功"
            lines.append(tag + _block(f"工具返回（{mark}，id={r.get('tool_call_id')}）",
                                      str(r.get("content", "")), full, max_chars))
        elif kind == "ModelUsage":
            lines.append(tag + "用量：" + _fmt_usage(data))
        elif kind == "RunFinished":
            msg = data.get("message") or {}
            lines.append(tag + _block("会话结束，最终消息",
                                      str(msg.get("content") or ""), full, max_chars))
        elif kind == "RunError":
            lines.append(tag + _block("出错", str(data.get("error", "")), full, max_chars))
        elif kind == "Progress":
            lines.append(tag + f"{data.get('scope')}：{data.get('text')}")
        else:
            # 未知类型不吞：框架加了新事件，这里也得把原始 data 交出去
            lines.append(tag + _block("原始 data",
                                      json.dumps(data, ensure_ascii=False),
                                      full, max_chars))
    return "\n".join(lines)


def _fact_group(fact: dict) -> tuple:
    return fact.get("failure"), fact.get("attempt")


def _fact_title(key: tuple) -> str:
    failure, attempt = key
    if failure is None:
        # baseline_failures、abort、dry_run 这些是 run 级的，不挂在任何一次
        # 尝试上。只渲染挂在 attempt 上的那些，恰恰会漏掉中止原因。
        return f"{_HR} 事实 · run 级 {_HR}"
    if attempt is None:
        return f"{_HR} 事实 · {failure} {_HR}"
    return f"{_HR} 事实 · {failure} · 第 {attempt} 次尝试 {_HR}"


def _render_facts(facts: list[dict], full: bool, max_chars: int) -> list[str]:
    """按 (failure, attempt) 归组。分组边界取 facts.jsonl 的原始先后顺序：
    run 级事实前后各有一批（开头 baseline_failures、结尾 abort），保持原序
    才不会把中止原因挪到最前面去。"""
    blocks: list[str] = []
    cur_key: tuple | None = None
    lines: list[str] = []
    for f in facts:
        key = _fact_group(f)
        if key != cur_key:
            if lines:
                blocks.append("\n".join(lines))
            cur_key, lines = key, [_fact_title(key)]
        lines.append("  " + _block(str(f.get("key")),
                                   _fmt_value(f.get("value")), full, max_chars))
    if lines:
        blocks.append("\n".join(lines))
    return blocks
