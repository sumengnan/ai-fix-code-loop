"""跑到一半的时候，用户看得见什么。

`run_once` 一次要跑几分钟起步：baseline 一次全量、每个 failure 两次模型调用、
每轮 verify 再一次全量。此前这几分钟里终端一个字都没有 —— `_cmd_run` 只在
`asyncio.run` 返回之后打一次报告，「在干活」和「卡死了」长得一模一样。

**方法名是语义，不是排版**：节点报告发生了什么，渲染由实现决定。这与
trace.py 里那条「事实是数据契约，报告是渲染」是同一条线 —— 让节点去拼字符串
的话，改一句措辞就要动核心循环。

默认实现 `NullProgress` 什么都不做，而且它是默认值：`eval` 会**并行**跑几十
个任务，每个都是一次完整的 run，默认出声的话几十条进度会交织成一团。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from typing import Any, TextIO


class NullProgress:
    """哑实现，也是 `run_once` 的默认值。

    方法体一律 `pass` 而不是 `raise NotImplementedError`：这个类的存在意义
    就是「被调用而不做事」，任何一个方法漏实现都会让不出声的调用方崩掉。
    """

    def run_start(self, run_id: str, adapter: str, branch: str) -> None: ...

    def baseline(self, ran: int, failing: int, seconds: float) -> None: ...

    def failure_start(self, index: int, total: int, test_id: str) -> None: ...

    def attempt_start(self, attempt: int, max_attempts: int) -> None: ...

    def detected(self, suspect: str | None, anchored: bool,
                 tokens: int) -> None: ...

    def agent_step(self, step: int, tool: str, arguments: dict) -> None: ...

    def agent_step_done(self, step: int, tool: str, ok: bool,
                        arguments: dict, result: str) -> None: ...

    def patched(self, touched: list[str], diff_lines: int) -> None: ...

    def verified(self, verdict: str, seconds: float) -> None: ...

    def note(self, text: str) -> None: ...

    def finished(self, fixed: int, total: int, tokens: int,
                 usd: float | None) -> None: ...


def _mmss(seconds: float) -> str:
    """秒数印成 `1:23` / `466s` 这种一眼能读的形状。

    不满一分钟就给秒：`0:07` 比 `7s` 多占位置又不多给信息。
    """
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


# ---------- 一行放得下：宽度与截断 ----------

# 硬上限 100 列，终端更窄就跟着窄。折行会把「就地重写」打回原形（`\r` 只回到
# 最后一个视觉行的行首），屏幕上留下一串对不齐的残句 —— 所以宁可截断。
_MAX_COLUMNS = 100
# 参数摘要的子预算：给失败原因留出位置。模型偶尔会发一条几百字符的正则，
# 不先给它封顶的话，整行预算被参数吃光，「为什么失败」被挤出屏幕 ——
# 而那恰恰是这一行存在的理由。
_ARGS_COLUMNS = 40


def _width(text: str) -> int:
    """显示宽度，中日韩字符按两格算。

    按 len() 截断的话，一行中文的实际宽度是算出来的两倍，照样折行。
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _cut(text: str, limit: int) -> str:
    """截到 limit 列以内，截过就带一个省略号 —— 让人看得出这里被截过。"""
    if limit <= 0:
        return ""
    if _width(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = _width(ch)
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _line_budget() -> int:
    import shutil
    return min(_MAX_COLUMNS,
               shutil.get_terminal_size((_MAX_COLUMNS, 24)).columns)


# ---------- 工具调用摘要：干了什么、结果如何、为什么失败 ----------
#
# 摘要住在这里而不是各个工具里：工具的返回值是**喂给模型**的正文，改它等于
# 改模型看到的东西。这一层只读那份正文，把它渲染成人看的一行 —— 与
# 「事实是数据契约，报告是渲染」是同一条线。


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _shorten_paths(text: str) -> str:
    """绝对路径只留最后两段。

    错误信息里的路径是 worktree 的绝对路径（`/Users/…/.aifix/runs/ab/tree/
    src/cart.py`），前缀每一行都一样、每一行都在占位置，真正有信息的是尾巴。
    """
    return re.sub(r"/(?:[^/\s'\"]+/){2,}([^/\s'\"]+/[^/\s'\"]+)", r"…/\1", text)


def _reason(text: str) -> str:
    """失败原因：首行，去掉框架加的前缀，路径压短。"""
    body = re.sub(r"^工具执行出错[:：]\s*", "", _first_line(text))
    return _shorten_paths(body)


def _diff_targets(diff: str) -> list[str]:
    """从 unified diff 里取出被改的文件（与 ApplyPatchTool 同源的读法）。"""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            path = line[4:].strip().split("\t")[0]
            if path in ("/dev/null", ""):
                continue
            if path[:2] in ("a/", "b/"):
                path = path[2:]
            if path not in out:
                out.append(path)
    return out


def _args_brief(tool: str, args: dict | None) -> str:
    """它对什么东西做了这件事。"""
    args = args or {}
    if tool == "apply_patch":
        targets = _diff_targets(str(args.get("diff", "")))
        if not targets:
            return ""
        return (targets[0] if len(targets) == 1
                else f"{targets[0]} 等 {len(targets)} 个文件")
    if tool == "run_tests":
        ids = args.get("test_ids") or []
        if len(ids) == 1:
            # 用例全名太长（前面 [1/2] 那一行已经给过完整 id），只留 :: 后那段
            return str(ids[0]).rsplit("::", 1)[-1]
        return f"{len(ids)} 个用例"
    if tool == "grep":
        where = str(args.get("path") or ".")
        return (f"/{args.get('pattern', '')}/"
                + ("" if where in (".", "") else f" 于 {where}"))
    if tool == "list_files":
        # path 有默认值 "."，模型不传是常态 —— 落到下面的兜底会印成
        # `language=python`（沙箱提示，不是内容），看起来像它在列一个叫
        # language 的东西
        return str(args.get("path") or ".")
    path = args.get("path")
    if path is not None:
        return str(path)
    # 未知工具（将来加的）也要说点什么，只剩一个光秃秃的名字没有用。
    # language 是给沙箱挑运行时用的，不是模型在操作的对象，不进这一行。
    return "、".join(f"{k}={v}" for k, v in args.items() if k != "language")


def _tests_brief(text: str) -> str:
    """`run_tests` 的收尾行。

    退出码非零**不是**工具失败：用例红了是它成功跑完的结果，这一行要说的
    正是红了几个。取不到收尾行时退回退出码，不编。
    """
    for line in reversed(text.splitlines()):
        if re.search(r"\d+ (passed|failed|error|skipped)|Tests run:", line):
            return re.sub(r"^[=\s]+|[=\s]+$", "", line)
    code = re.search(r"^exit_code=(-?\d+)", text, re.M)
    return f"exit_code={code.group(1)}" if code else ""


def _patch_brief(text: str) -> str:
    ins = re.search(r"(\d+) insertion", text)
    dele = re.search(r"(\d+) deletion", text)
    parts = ([f"+{ins.group(1)}"] if ins else []) + \
            ([f"-{dele.group(1)}"] if dele else [])
    return " ".join(parts) if parts else "已应用"


def _result_brief(tool: str, ok: bool, result: str) -> str:
    """结果如何 —— 失败时就是为什么失败。"""
    text = result or ""
    if not ok:
        return _reason(text)
    if tool == "run_tests":
        return _tests_brief(text)
    if tool == "read_file":
        return f"{len(text.splitlines())} 行"
    if tool == "list_files":
        stripped = text.strip()
        if not stripped or stripped == "（空目录）":
            return "空目录"
        return f"{len(stripped.splitlines())} 项"
    if tool == "grep":
        if text.strip().startswith("无匹配"):
            return "无匹配"
        hits = [ln for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("…")]
        return f"{len(hits)} 处匹配"
    if tool == "apply_patch":
        return _patch_brief(text)
    return _shorten_paths(_first_line(text))


class StepReporter:
    """把事件流上的工具调用编号，开始与结束各报一次。

    编号按 **tool_call_id** 认领，不是「结束一次加一」：一个 step 可以并发
    发起多条工具调用（实跑过的第 1 步就同时发了两条 read_file），先返回的
    那条会顶掉另一条的号，屏幕上谁勾谁叉全错位。

    计数器跨多次 `consume` 复用 —— 守卫重试是同一次修复尝试的延续，每轮从
    1 重数会让屏幕上出现两段「第 1 步」，读起来像是重新开始了。
    """

    def __init__(self, progress: Any) -> None:
        self._p = progress
        self._n = 0
        self._numbers: dict[str, int] = {}

    def started(self, call: Any) -> None:
        self._n += 1
        self._numbers[call.id] = self._n
        self._p.agent_step(step=self._n, tool=call.name,
                           arguments=call.arguments)

    def finished(self, call: Any, result: Any) -> None:
        # 没见过开始的结束事件不报：步号是开始时发出去的，对着一个从没编过号
        # 的调用报结束，屏幕上会冒出一个与上下文对不上的步号。
        step = self._numbers.pop(call.id, None)
        if step is None:
            return
        self._p.agent_step_done(step=step, tool=call.name,
                                ok=not result.is_error,
                                arguments=call.arguments,
                                result=result.content)


class TerminalProgress(NullProgress):
    """渲染到 **stderr**。

    走 stderr 而不是 stdout 是决定性的：报告走 stdout，而 `aifix run . >
    report.md` 是最常见的用法（把报告存下来、贴进 PR）。混在一起的话，存出来
    的 report.md 顶上会粘着几十行进度。

    每条都 flush：stderr 在终端上是行缓冲，但**重定向到文件时会变成块缓冲**
    （4KB 起），而一次 run 的全部进度远不到 4KB —— 不 flush 的话它们会攒到
    进程退出才一次吐出来，等于没做。`mine` / `eval` 那几处 print 显式带
    flush=True 是同一个理由。
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        # 默认值在调用时取而不是在这里绑定 sys.stderr：pytest 的 capsys 会
        # 替换 sys.stderr，构造得早一点就写到了被替换之前的那个流上。
        self._stream = stream
        # 屏幕上是否还挂着一条没收尾的「进行中」半行（只在终端上出现）
        self._pending = False

    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def _tty(self) -> bool:
        try:
            return bool(self._out().isatty())
        except Exception:      # 流不支持 isatty（少见的包装流）时按非终端处理
            return False

    def _w(self, line: str) -> None:
        out = self._out()
        # 半行还挂着就先收尾，否则下一条会粘在它屁股后面
        if self._pending:
            out.write("\n")
            self._pending = False
        out.write(line + "\n")
        out.flush()

    def run_start(self, run_id: str, adapter: str, branch: str) -> None:
        # run_id 放在最前面：产物、replay、交付分支全都按它索引，用户在
        # 第一秒就该知道待会儿去哪儿看
        self._w(f"aifix run {run_id} · 适配器 {adapter} · 分支 {branch}")

    def baseline(self, ran: int, failing: int, seconds: float) -> None:
        # 同时给「跑了多少」和「红了多少」：只给红的数目分不出 2/14 还是
        # 2/2000，而后者意味着接下来每轮 verify 都要再跑一次那 2000 个，
        # 用户看到的停顿会长得多 —— 而那是正常的
        self._w(f"baseline：{ran} 个用例，{failing} 个红的（{_mmss(seconds)}）")

    def failure_start(self, index: int, total: int, test_id: str) -> None:
        self._w(f"[{index}/{total}] {test_id}")

    def attempt_start(self, attempt: int, max_attempts: int) -> None:
        self._w(f"      第 {attempt}/{max_attempts} 轮")

    def detected(self, suspect: str | None, anchored: bool,
                 tokens: int) -> None:
        where = suspect or "（未给出）"
        # 无锚点要说出来：那种诊断是模型按包名猜的，用户看到它指错文件时
        # 才知道这不是模型笨，是 traceback 里压根没有源码帧
        mark = "" if anchored else "，无源码锚点"
        self._w(f"      诊断：{where}{mark}  {tokens:,} tokens")

    def _step_line(self, mark: str, step: int, tool: str,
                   brief: str, tail: str) -> str:
        body = f"第 {step} 步 {tool}"
        if brief:
            body += f"  {_cut(brief, _ARGS_COLUMNS)}"
        if tail:
            body += f" → {tail}"
        # 前缀是 6 个空格 + 标记 + 空格 = 8 列，标记本身永远是一格宽的符号
        return f"      {mark} " + _cut(body, _line_budget() - 8)

    def agent_step(self, step: int, tool: str, arguments: dict) -> None:
        """工具**开始**跑：只在终端上印，待会儿被结果那一行就地重写掉。

        勾还是叉要等工具跑完才知道，而 run_tests 一跑几十秒 —— 那几十秒
        不能是空屏，这半行就是心跳。

        重定向到文件时一个字都不印：`\\r` 重写在文件里不生效，进行中那半行
        会原样留下，一次 run 的日志里凭空多出二十几行残句；而且那时也没人
        盯着屏幕看。
        """
        if not self._tty():
            return
        out = self._out()
        if self._pending:      # 上一条没收尾（不该发生），先断开
            out.write("\n")
        out.write(self._step_line("·", step, tool,
                                  _args_brief(tool, arguments), "运行中…"))
        out.flush()
        self._pending = True

    def agent_step_done(self, step: int, tool: str, ok: bool,
                        arguments: dict, result: str) -> None:
        """工具跑完：成功打绿勾、失败打红叉，后面跟上它干了什么、结果如何。

        标记放在**工具名前面**，一列对齐 —— 跟在名字后面的话，工具名长短
        不一，勾和叉会散落在各个列上，「哪一步栽了」又得逐行读。

        `ok` 来自 `ToolResult.is_error`，**不是**退出码：`run_tests` 报告用例
        红了是它成功跑完的结果。按退出码判的话屏幕上会出现一片红叉，而模型
        正在正常工作，真正的失败（守卫拒绝、补丁打不上）淹没在里面。
        """
        mark = "✓" if ok else "✗"
        line = self._step_line(mark, step, tool,
                               _args_brief(tool, arguments),
                               _result_brief(tool, ok, result))
        if self._tty():
            # 颜色只给终端：进度走 stderr，而 `2> run.log` 是真实用法，
            # ANSI 码写进文件就是一串 ESC[32m 垃圾，grep 出来的行也对不上。
            color = "\033[32m" if ok else "\033[31m"
            line = line.replace(mark, f"{color}{mark}\033[0m", 1)
            out = self._out()
            # 回到行首并清掉整行：不清的话，短的结果行盖不住长的「进行中」，
            # 会留下前一行的尾巴
            out.write(("\r\033[2K" if self._pending else "") + line + "\n")
            out.flush()
            self._pending = False
            return
        self._w(line)

    def patched(self, touched: list[str], diff_lines: int) -> None:
        if not touched:
            self._w("      改动：无")
            return
        head = "、".join(touched[:3])
        more = f" 等 {len(touched)} 个文件" if len(touched) > 3 else ""
        self._w(f"      改动：{head}{more}（{diff_lines} 行）")

    def verified(self, verdict: str, seconds: float) -> None:
        # 措辞与报告同源：两处各写一份的话，早晚有一处跟另一处说得不一样
        from .nodes.report import _VERDICT_CN
        self._w(f"      验证：{_VERDICT_CN.get(verdict, verdict)}"
                f"（{_mmss(seconds)}）")

    def note(self, text: str) -> None:
        self._w(f"      ⚠️  {text}")

    def finished(self, fixed: int, total: int, tokens: int,
                 usd: float | None) -> None:
        # usd 为 None 表示没配价格表 —— 印 $0.00 就是伪造，见 report.cost_is_unknown
        cost = "成本未知" if usd is None else f"${usd:.4f}"
        self._w(f"完成：修复 {fixed}/{total} · {tokens:,} tokens · {cost}")
