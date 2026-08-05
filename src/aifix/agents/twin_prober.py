"""给一对「疑似两处实现同一件事」的函数写对比代码。

产出不是一句「我觉得这里有问题」，而是**一段能跑的测试**：同一份输入喂给两边，
断言结果相等。跑出来不相等 → 确认了一个缺陷；相等 → 这个候选作废。

这样安排的理由是：一个不能被便宜地证伪的候选就是噪音。让模型输出自然语言的
可疑点，人查两次发现都是假的，之后整个功能就废了。

`Reproduction` 原样复用（连同它的自包含校验）：对比测试同样要落进一个新文件，
同样必须自包含，`target_test_id` 同样要能追溯到 `test_file`。
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Callable

from pydantic import BaseModel, ValidationError

from .reproducer import (Harness, Reproduction, _harness_section, _incoherence,
                         _last_object)

SYSTEM_PROMPT = """你是一个把「两处代码可能不一致」变成一条可执行测试的工程师。

给你两个函数，它们**可能**在做同一件事（判据是：它们分支在同一组字符串
字面量上，而且函数名有共同词根）。这个判据是启发式的，会配错。

你的任务只有一个：**写一条测试，给两边喂同一份输入，断言它们的结果相等。**

不要写「我觉得这里有问题」。这一层不接受观点，只接受一段能跑的代码 ——
跑出来不相等才算发现了缺陷，相等就说明这个候选作废。

先用工具读那两个函数的完整定义，确认参数怎么传、返回什么。两边的签名往往
不同（一个是方法、一个是自由函数；参数名不一样；有的多一个可选参数），
你要负责把同一份输入翻译成两边各自的调用形式。

**挑最可能分岔的输入**，不要挑最平常的那个。分岔通常藏在边界上：重复元素、
顺序不同、空值、None、越界下标、类型不同但语义相同的值。喂一份两边显然都
处理得了的输入，测试会绿，而那什么也没证明。

**它们不是同一件事时，如实说。** can_probe 填 false，并在 not_comparable_why
里写清为什么（返回类型不同、一个渲染文本一个判对错、一个有副作用另一个纯粹
计算……）。硬编一个对比出来，等于凭空造一个假缺陷 —— 那比什么都不报更糟。

只输出一个 JSON 对象：
- can_probe: 布尔。两者确实在做同一件事、能写出对比测试时为 true
- test_file: 新测试文件的路径（相对 repo 根），必须落在测试目录之下
- test_code: 完整的测试文件内容。**自包含**：用到的每个名字都在这份代码里
  import 或定义过（包括 pytest），不依赖任何既有测试文件
- target_test_id: 这条用例的完整标识，格式与本项目其余用例一致
- not_comparable_why: can_probe 为 false 时，一句话说清为什么不可比

硬约束：
- 只写一条测试函数
- 断言必须是**两边结果相等**，不是断言某一边等于某个你猜的期望值 ——
  你不知道正确答案是什么，你只知道它们应该一致
- 不要修改任何已有文件
"""


class TwinProbe(BaseModel):
    can_probe: bool
    test_file: str | None = None
    test_code: str | None = None
    target_test_id: str | None = None
    not_comparable_why: str = ""


def build_prompt(twin, harnesses: Sequence[Harness]) -> str:
    """把候选、**配对依据**、以及测试体系的写法一起给出去。

    依据不能省：模型在这一层唯一能做的判断就是「这两个是不是真的一回事」，
    而不告诉它为什么被配到一起，它只能凭函数名猜。

    id 样例同样不能省（复用 reproducer 的 `_harness_section`）。实测只说
    「格式与本项目其余用例一致」时，模型写出的 target_test_id 与落盘的用例
    对不上，红检报「没有跑出任何结果」—— 77k tokens 白烧，而它其实已经把
    测试写对了。
    """
    return (
        f"这两个函数可能在做同一件事：\n\n"
        f"  A  {twin.a.path}:{twin.a.lineno}  {twin.a.name}\n"
        f"  B  {twin.b.path}:{twin.b.lineno}  {twin.b.name}\n\n"
        f"配对依据：\n"
        f"  共同词根：{'、'.join(sorted(twin.shared_roots))}\n"
        f"  都在这些字面量上分支：{'、'.join(sorted(twin.shared_literals))}\n\n"
        + _harness_section(harnesses))


def parse_probe(raw: str,
                is_test: Callable[[str], bool]) -> tuple[Reproduction | None, str]:
    """解析成 `Reproduction`（下游原样复用复现那条通路），并说清为什么不成。

    说了不可比却不说为什么 → 判不自洽：那句说明是这条通路唯一的产出，它决定
    这一对要不要从候选里永久划掉。
    """
    for text in (raw, _last_object(raw)):
        if text is None:
            continue
        try:
            p = TwinProbe.model_validate_json(text)
        except ValidationError:
            continue
        if not p.can_probe:
            if not p.not_comparable_why.strip():
                return None, "说了不可比，却没说为什么"
            return Reproduction(can_reproduce=False,
                                missing_info=[p.not_comparable_why]), ""
        r = Reproduction(can_reproduce=True, test_file=p.test_file,
                         test_code=p.test_code,
                         target_test_id=p.target_test_id)
        why = _incoherence(r, is_test)
        return (None, why) if why else (r, "")
    return None, "输出里找不到一个能解析的 JSON 对象"
