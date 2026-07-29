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

import sys
from typing import TextIO


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

    def agent_step(self, step: int, tool: str) -> None: ...

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

    def _w(self, line: str) -> None:
        out = self._stream if self._stream is not None else sys.stderr
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

    def agent_step(self, step: int, tool: str) -> None:
        self._w(f"      · 第 {step} 步 {tool}")

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
