"""`ask_user`：信息不全时**停下来问人**，而不是猜一个答案改下去。

什么时候该问、什么时候不该，这条线必须画清楚，否则这个工具会变成模型逃避
工作的出口：

- **改的是「怎么做到」→ 自己试。** 几种实现都能让测试变绿时，判定权在 verify
  那一步（零 LLM、三态判决）。既然有确定性裁判，就不该让人在两段没验证过的
  代码之间选。
- **改的是「什么才算对」→ 必须问。** 「购物车为空时返回 None 还是抛异常」是
  产品决策，读多少代码都推不出来。

三道约束都是硬判，不靠提示词：

1. **必须先读过代码。** 一次工具都没调就说「信息不全」，那多半是它自己没查。
2. **一次 run 只能问一次。** 不设上限的话，问问题是比修 bug 便宜得多的动作。
3. **必须给选项，不能开放式。** 这一条约束的是**模型**，不是人 —— 它逼着
   模型在被允许停下之前，把一个开放问题收敛成几个具体行为。松掉它，`ask_user`
   会退化成「我卡住了，救命」，而那正是上面两条在拦的东西。

   **人怎么回答不受这条管**：可以回编号，也可以直接用自己的话。答复只是被拼
   进下一轮的提示词（见 `agents/fixer.format_answer`），没有第二次模型调用、
   没有独立的意图解析步骤。这里从前写着「开放式回复要再过一次模型去解析
   意图」，那描述的是一种本代码库里并不存在的实现 —— 而整个系统的前提本来
   就是读一段自由文本的缺陷报告然后改代码。

   编号形态仍然值得留着：它选的就是模型自己列的第 N 项，这条审计记录是无歧义
   的，自由回答没有这个性质（所以自由回答会被原样记进 trace）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from harness.tools.base import Tool, ToolError

# 读过其中任何一个才算「查过了」。run_tests 不在内：跑一遍测试不等于看过代码，
# 而它恰恰是模型最容易用来充数的那个动作。
_READ_TOOLS = frozenset({"read_file", "read_symbol", "grep", "list_files"})


@dataclass
class Pending:
    """本次 run 待人回答的问题。空 question 表示没人问过。

    可变持有者，不是返回值：工具在 AgentLoop **内部**被调用，而决定「这次
    run 要不要就此停下」的是 fix_node，在循环之外。中间隔着框架的调用栈，
    传不回去，只能共用一个对象。
    """
    test_id: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)

    @property
    def asked(self) -> bool:
        return bool(self.question and self.options)

    def to_dict(self) -> dict:
        return {"test_id": self.test_id, "question": self.question,
                "options": list(self.options)}


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "信息不全、无法判断**什么才算正确行为**时，停下来问人。"
        "必须给出 2-4 个具体选项 —— 人回一个编号就能继续"
        "（也可以直接用自己的话答，但选项仍然必须给）。"
        "注意：几种改法都能让测试变绿时**不要问**，自己选一个试，"
        "由 run_tests 判对错；只有当不同选项意味着**不同的期望行为**"
        "（属于产品决策）时才用这个工具。一次 run 只能问一次，"
        "问之前必须先读过相关代码。")

    class Params(BaseModel):
        question: str = Field(
            min_length=1,
            description="要问的问题，一句话说清楚卡在哪，不要复述代码")
        options: list[str] = Field(
            min_length=2, max_length=4,
            description="2-4 个具体选项，每条写清「选它会发生什么」")

    def __init__(self, pending: Pending, seen_tools: set[str],
                 test_id: str = "") -> None:
        self._pending = pending
        # fix_node 通过 on_tool 回调往里塞工具名。共享**同一个集合对象** ——
        # 传副本的话这里永远看到空集，第一条约束会把每一次提问都拦死。
        self._seen = seen_tools
        self._test_id = test_id

    async def run(self, params: "AskUserTool.Params") -> str:
        if not (self._seen & _READ_TOOLS):
            raise ToolError(
                "你还没有读过任何代码就要提问。"
                "先用 read_symbol / read_file / grep 把相关实现看一遍 —— "
                "「信息不全」在多数情况下是还没查，而不是真的查不到。")
        if self._pending.asked:
            raise ToolError(
                "这次运行已经问过一个问题了，一次只能问一个。"
                "请就着现有信息做出最合理的判断并动手修复。")
        if len(set(o.strip() for o in params.options)) != len(params.options):
            raise ToolError("选项里有重复的。每个选项要对应一种**不同的**期望行为。")

        self._pending.test_id = self._test_id
        self._pending.question = params.question.strip()
        self._pending.options = [o.strip() for o in params.options]
        return ("问题已记录，本次运行到此为止 —— **现在就停下，不要再调用任何"
                "工具，也不要再做修改**。人回答之后会带着答案重新跑一次。")
