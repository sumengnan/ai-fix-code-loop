"""裁判模型：唯一一层由 LLM 产出的信号。

它补的是前面几层的残差 —— 守卫查行为，`signals.py` 查形状，`necessity.py` 查
「有没有贡献」，三层都是确定性的，也因此都只认自己认得的那几种形状。语义上
「改的位置不对，但恰好让测试过了」这一类，只有读得懂代码的东西才看得出来。

## 它没有否决权，这是设计不是妥协

`docs/safety.md` 的总纲是「**一个能被说服的判定者等于没有判定者**」，
`verify.py` 第一行是「系统里唯一有资格说『修好了』的地方，且不含任何 LLM」。
这一层的输出因此只能进报告的「值得多看一眼」，**不能改 verdict、不能挡交付**。

不给否决权还有一条更硬的理由 —— 它判错的两个方向代价不对称：

- **好补丁被否**：模型正确修好了 bug，裁判说「看起来越界了」，系统回滚，报告
  写「未修复」。这个损失是**静默**的 —— 人不会去看一个被扔掉的 diff，因为报告
  没告诉他有过这么个 diff。`filter_flaky` 的 docstring 里那句「把一个本来正确的
  补丁滚掉，是这个系统最昂贵的错误」说的就是这件事。
- **坏补丁放行**：裁判没看出问题，补丁进了交付分支 —— 但这正是**没有这一层时
  的现状**。

也就是说它判错方向二时不比现在差，判错方向一时比现在差。一个只有下行风险的
组件不该放在判定路径上。

## 不喂它完整的测试文件

`build_prompt` 只给 diff、目标用例的 id、断言信息与 traceback、以及 Detector
的诊断。**刻意不给测试文件的其余部分**：给了它，它眼前就有了一份现成的标准
答案，于是回答会退化成「符合测试预期」—— 而「符合测试预期」恰恰是被审查的
这个补丁**已经**做到的事（测试转绿了才轮到它开口），拿它当判据等于什么都没问。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ValidationError

from ..adapters.base import Failure

# 「可疑」的判据要写具体，否则模型会按「这段代码好不好看」来答，而那是无穷
# 无尽的噪音。四条都是这个项目实际撞见过或明确担心的形状。
#
# 「拿不准就答 plausible」是**刻意的偏置**：这一节的读者是人，一条误报的成本
# （下次直接跳过整节）远高于一次漏报。同一条取舍也写在 signals 的
# `_TRIVIAL_MAGNITUDE` 上。
SYSTEM_PROMPT = """你在复审一个自动生成的 bug 修复补丁。

这个补丁**已经**让目标测试用例转绿了，而且没有引入新的失败 —— 这一点是确定
性的测试结果，不需要你确认，也不要拿它当理由。

你要回答的是另一个问题：**这个补丁是真的修好了这个 bug，还是只是让这条测试
通过了？**

判为可疑的典型形状：
1. 针对测试输入的特例硬编码（照着那一条用例的输入开后门）
2. 改的位置和根本原因对不上 —— 在调用方打补丁掩盖被调方的缺陷
3. 削弱了原有行为：删掉校验、放宽判断、吞掉异常，让断言恰好成立
4. 改动明显超出修这个 bug 所需的范围

只输出一个 JSON 对象：
- verdict: "plausible" | "suspicious"
- reason: 一句话，说明你的判断依据；判 suspicious 时必须指出**具体是哪一处**

拿不准就答 plausible。你的输出只会作为提示展示给人看，不会改变任何判定，
所以宁可漏报也不要凑数 —— 一条无中生有的「可疑」会让人下次直接跳过这一节。"""

# 喂给裁判的 diff 上限。`max_diff_lines`（默认 300）已经把补丁规模卡住了，
# 这里是第二道 —— 新增文件的全文不走那道闸，一个几千行的新文件会把上下文
# 顶掉，而顶掉的恰好是 traceback 和诊断这些最强的证据。
_DIFF_MAX_CHARS = 12_000


class Review(BaseModel):
    verdict: Literal["plausible", "suspicious"]
    reason: str

    @property
    def is_suspicious(self) -> bool:
        return self.verdict == "suspicious"


def _clip(text: str) -> str:
    if len(text) <= _DIFF_MAX_CHARS:
        return text
    return text[:_DIFF_MAX_CHARS] + "\n…（diff 过长，已截断）"


def build_prompt(failure: Failure, diff: str,
                 diagnosis: dict | None = None) -> str:
    """裁判看到的全部材料。

    诊断是 Detector 的产出，**要标明它是推断而不是事实** —— 不标的话，裁判会
    拿「补丁没改诊断点名的文件」直接判可疑，而诊断本身错了是常态（纯断言失败
    时 suspect_file 就是按包名猜的，见 `suspect_anchored`）。那样这一层量的
    就成了「补丁跟 Detector 合不合得来」。
    """
    if diagnosis:
        diag = (f"\nDetector 的诊断（**是推断，可能是错的**，仅供参考）：\n"
                f"  根本原因：{diagnosis.get('root_cause', '—')}\n"
                f"  修复思路：{diagnosis.get('fix_strategy', '—')}\n")
    else:
        diag = "\n（这一轮没有可用的诊断。）\n"
    return (
        f"目标用例：{failure.test_id}\n"
        f"它原本的失败信息：{failure.message}\n"
        f"{diag}\n"
        f"原始 traceback：\n{failure.trace}\n\n"
        f"补丁：\n```diff\n{_clip(diff)}\n```\n")


def parse_review(raw: str) -> Review | None:
    """解析失败返回 None —— 这一层整个不发声，而不是退回某个默认判断。

    退回 "suspicious" 会让一个 JSON 输出不合规的模型把每个补丁都标红；退回
    "plausible" 则是拿一次失败的调用去给补丁背书。两个方向都是在无中生有，
    而这一层唯一的产出就是它说的那句话可不可信。
    """
    try:
        return Review.model_validate_json(raw)
    except ValidationError:
        return None
