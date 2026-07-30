"""按**行范围**读文件。替掉框架那个只有 `path` 的 ReadFileTool。

为什么自己写一份（实测逼出来的，2026-07-30，issue #2 的真跑）：

模型第一次调用就直奔正确的文件 `src/aifix/cli.py`（46,447 字符），grep 到了
目标函数在第 519 行 —— 然后把同一个文件**读了五遍**，每遍都拿回一模一样的前
8000 字符，最后 token 额度耗尽、零产出。

框架的 `ReadFileTool` 只有 `path` 一个参数，截断消息是「…(已截断)」：它告诉
模型内容被切了，却**不给任何拿到剩下部分的办法**。于是重读成了模型唯一能想
到的动作，而重读永远拿回同一段。它的行为是理性的，残缺的是工具。

**这不只是 M6 的问题。** fixer 用的是同一个工具面，而这个仓库 48 个源文件里
有 18 个超过 8000 字符 —— 它们 200 行之后的缺陷，对整个系统都够不着。

两处刻意的设计：

- **带行号。** 模型要靠它写 `apply_patch` 的上下文，也要靠它判断从哪续读。
- **截断消息带下一个 offset。** 「已截断」只说明发生了什么，不说下一步；这个
  项目对静默截断一向敏感（守卫、预算、报告都吃过亏），而这里更进一步：不给
  出路的截断会把模型逼进死循环。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from harness.sandbox.base import Sandbox, resolve_in_workspace
from harness.tools.base import Tool, ToolError


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读工作区里的文件，返回带行号的内容。"
        "文件大时用 offset/limit 分段读 —— 截断消息里会给出下一段的 offset。")

    class Params(BaseModel):
        path: str = Field(description="相对工作区根目录的路径")
        offset: int = Field(default=1, ge=1,
                            description="从第几行开始读（1 起）")
        limit: int = Field(default=0, ge=0,
                           description="最多读几行；0 表示读到字符上限为止")

    def __init__(self, sandbox: Sandbox, max_chars: int = 8000) -> None:
        self._sandbox = sandbox
        self._max_chars = max_chars

    async def run(self, params: "ReadFileTool.Params") -> str:
        # resolve_in_workspace 返回的是**字符串**（围栏规约后的路径），
        # 不是 Path —— 这一点踩过一次
        p = Path(resolve_in_workspace(self._sandbox.workspace, params.path))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise ToolError(f"文件不存在：{params.path}") from None
        except IsADirectoryError:
            raise ToolError(f"这是个目录，不是文件：{params.path}") from None

        lines = text.splitlines()
        total = len(lines)
        if params.offset > total:
            # 返回空串的话，模型分不出「这个文件到头了」和「读失败了」——
            # 而这两种情况的下一步完全不同。
            return (f"（{params.path} 共 {total} 行，offset={params.offset} "
                    f"已越过末尾。）")

        start = params.offset - 1
        end = total if params.limit == 0 else min(total, start + params.limit)

        out: list[str] = []
        used = 0
        stopped_at = end
        for i in range(start, end):
            row = f"{i + 1:>6}\t{lines[i]}"
            # 先判再加：加完再判会让最后一行超出上限，而调用方给的是硬上限
            if used + len(row) + 1 > self._max_chars:
                stopped_at = i
                break
            out.append(row)
            used += len(row) + 1
        else:
            stopped_at = end

        body = "\n".join(out)
        if stopped_at < total:
            # **可操作**的截断：给出下一段从哪开始。stopped_at 是「已读到的最后
            # 一行」的下标（0 起），所以下一行的行号正好是 stopped_at + 1。
            # 差一行的后果是静默的：模型照着提示续读，中间少一行代码而不自知。
            body += (f"\n…（已截断：{params.path} 共 {total} 行，本次到第 "
                     f"{stopped_at} 行。续读：offset={stopped_at + 1}）")
        return body or f"（{params.path} 是空文件）"
