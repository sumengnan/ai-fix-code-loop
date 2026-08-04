"""结算货币：一律人民币。

价表可以按美元填，也可以按人民币填（`AIFIX_PRICE_CURRENCY`）——厂商公开的
价目表大多是 USD / 1k token，抄过来不用换算，少一次手算错小数点的机会。但
**闸、预算、报告、库里的数只有一种货币**：人民币。两种货币同时流到下游的话，
「这次比上次贵」这种最基本的比较都要先问一句「哪一次是美元」，而那个问题在
一个 float 上是问不出答案的。

折算发生在**唯一的入口**：`agents/runner.consume` 把框架事件里的 cost 累加
进 `AgentOutcome` 的那一处。从那里往后（成本闸、state、报告、trajectory、
eval）流的全是人民币，没有第二个地方需要知道汇率。多一处折算就多一处「折了
两次」的机会，而折两次的数字看起来完全正常。

例外只有 `replay`：它渲染的是**事件流里原样落盘的数**，那份产物写的是价表
货币。那一层同样用这里的 Money 折算，理由是人读回放和读报告时不该在心里做
一次汇率换算才对得上。
"""
from __future__ import annotations

from dataclasses import dataclass

USD = "USD"
CNY = "CNY"
CURRENCIES = (USD, CNY)

# 默认汇率。**是个约数，不是实时值** —— 这个项目的预算是拿来跨模型、跨时间
# 对比的（见 budget.RunBudget.exhaustion），实时汇率会让同一批 eval 隔天跑出
# 来的成本不可比，那正是这套预算设计要避免的事。所以它是一个写死的、可配的、
# 会在报告里明写出来的数：读的人知道它是约数，就不会拿它去对账。
DEFAULT_USD_TO_CNY = 7.2


@dataclass(frozen=True)
class Money:
    """价表货币 → 人民币的折算规则。

    从 `AifixConfig.money` 拿，不要在别处自己拼一个：默认值散落两处之后，
    「报告里的数」和「闸拦下来的数」迟早会用上不同的汇率，而两个数都还是
    「看着正常」。
    """

    price_currency: str = USD
    usd_to_cny: float = DEFAULT_USD_TO_CNY

    @property
    def rate(self) -> float:
        """价表货币折成人民币的乘数。价表本来就是人民币时是 1，不是 7.2。"""
        return 1.0 if self.price_currency == CNY else self.usd_to_cny

    def to_cny(self, amount: float) -> float:
        return amount * self.rate

    def rate_note(self) -> str:
        """报告里那句「按什么汇率折的」。价表已是人民币时没有汇率可言，返回空串。

        必须印出来：一个不写汇率的人民币金额会被当成实时汇率折的，而它是
        个写死的约数（见 DEFAULT_USD_TO_CNY）。差别在对账的时候才暴露，
        那时已经晚了。
        """
        if self.price_currency == CNY:
            return ""
        return f"按 1 USD = {self.usd_to_cny:g} CNY 折算"


def fmt_cny(v: float) -> str:
    """小额不四舍五入成 ¥0.00 —— `--budget 0.001` 显示成上限 ¥0.00 像个 bug。"""
    return f"¥{v:.2f}" if abs(v) >= 0.01 else f"¥{v:g}"
