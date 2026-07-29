from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from ..adapters.base import Failure, SourceCandidate

SYSTEM_PROMPT = """你是一个代码缺陷定位专家。

给你一个失败的测试用例和从栈帧还原出的嫌疑源码位置，你要判断真正的缺陷在哪、为什么。
你没有任何工具，只能依据给出的信息推断。

只输出一个 JSON 对象，字段如下：
- suspect_file: 字符串，最可疑的源码文件（相对 repo 根的路径）
- suspect_lines: [起始行, 结束行] 或 null
- root_cause: 字符串，缺陷的根本原因
- fix_strategy: 字符串，修复思路（不要写出完整代码）
- confidence: "high" | "medium" | "low"

注意：栈帧最深处未必是缺陷所在，它可能只是断言失败的位置。"""


class Diagnosis(BaseModel):
    suspect_file: str
    suspect_lines: tuple[int, int] | None = None
    root_cause: str
    fix_strategy: str
    confidence: Literal["high", "medium", "low"]


def build_prompt(failure: Failure, candidates: list[SourceCandidate]) -> str:
    """候选的来源必须写在行里，不能只列路径。

    纯断言失败下候选来自测试文件的 import（`SourceCandidate.origin`），证据
    比栈帧弱一档：「测试 import 了这个模块」不等于「缺陷在这个模块」。不标出
    来，模型会把两者当同一回事 —— 而这些路径**是准的**（确定性推出来的），
    于是它会拿一个只说明「用到了」的事实去支撑「缺陷在这」的结论。

    `path` 一律照原样给出（相对 repo 根）。让模型自己改写路径形式，就是把
    定位准确率变成书写风格的函数 —— 那个坑记在 eval/runner.locate_hit 里。
    """
    if candidates:
        cand_text = "\n".join(
            f"  {i + 1}. {c.path}:{c.line}  在 {c.frame or '?'}()"
            f"{'' if c.origin == 'traceback' else '  ← 测试 import 的模块，非栈帧'}"
            for i, c in enumerate(candidates))
    else:
        cand_text = "  （未能从栈帧定位到 repo 内的源码）"
    return (
        f"失败用例：{failure.test_id}\n"
        f"断言信息：{failure.message}\n\n"
        f"嫌疑位置（按可疑度排序，最深的栈帧在前）：\n{cand_text}\n\n"
        f"suspect_file 请从上面的路径里原样选一条，不要改写形式。\n\n"
        f"完整 traceback：\n{failure.trace}\n")


def parse_diagnosis(raw: str) -> Diagnosis | None:
    """解析失败返回 None —— 这是降级信号，调用方改为把原始 traceback 交给 Fixer。"""
    try:
        return Diagnosis.model_validate_json(raw)
    except ValidationError:
        pass
    # 有些端点会在 JSON 外包一层围栏或解释文字，退一步尝试抽取首个对象
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return Diagnosis.model_validate(json.loads(raw[start:end + 1]))
    except (ValidationError, json.JSONDecodeError):
        return None
