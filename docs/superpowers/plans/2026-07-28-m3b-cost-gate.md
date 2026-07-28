# M3b 成本闸 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让「我给这个工具设的花钱上限」成为一句可信的话。

**架构：** 在 `consume()` 里按真实成本熔断（事件级、零框架改动）；`RunBudget` 补美元的动态分配；`fix_node` 加「同一守卫连续 N 次即放弃」；`run_suite` 加整批总闸，未跑的任务落成 `error` 而非失败；显式要求美元上限却无价格表时启动即拒绝。

**技术栈：** Python ≥3.11 · `ai-harness-framework` 0.0.2（不改） · pydantic v2 · asyncio · pytest

**规格：** `docs/superpowers/specs/2026-07-28-m3b-cost-gate-design.md`
**前置：** M3 已完成并推送（`main` @ `e071343`，253 passed）

## 起点：M3 真实验收跑出的三个数字

| # | 现象 | 根因 |
|---|---|---|
| 1 | `--budget 0.60` 花掉 `$1.60` | 预算只在「取下一个 failure 之前」检查，单任务评测里那次检查在开跑前就过了 |
| 2 | 两个模型各烧 51~52 万 token 一个字没改 | 空 diff 守卫把 `fix_guard_retries` 用满，每轮都是完整 AgentLoop |
| 3 | `aifix eval` 无任何预算参数，20 任务上限 $40 | 每任务各自建预算，整批无闸 |

## 与规格的偏差（自检发现，已确认）

规格 §5.1 列了三个咬合点，其中「单次 AgentLoop」这一层**只接进 `fix_node`，不接 `detect_node`**。

理由：detect 是单步、无工具、已被 `detector_max_tokens = 20_000` 硬限住的一次调用，钱不在那里烧；给它再叠一层美元闸只增加接线面，不增加安全性。`fix_node` 才是唯一可能跑几十步、反复重试的地方。

## 文件结构

**修改**

| 文件 | 变更 |
|---|---|
| `src/aifix/agents/runner.py` | `AgentOutcome.cost_capped`；`consume(stream, cost_cap=)` |
| `src/aifix/budget.py` | `remaining_usd()` / `usd_for_failure()` |
| `src/aifix/config.py` | `guard_giveup_limit` |
| `src/aifix/nodes/fix.py` | 接成本闸；同一守卫连续 N 次即放弃 |
| `src/aifix/graph.py` | `AifixState` 补 `failure_usd_budget` |
| `src/aifix/cli.py` | 单 failure 美元分配接线；启动校验；`eval` 两个预算参数；`--help` 写契约 |
| `src/aifix/eval/runner.py` | `run_suite` 整批总闸 |

**框架侧：无改动。**

---

# 阶段 1：单次 AgentLoop 的成本闸

### 任务 1：`consume()` 按真实成本熔断

**文件：**
- 修改：`src/aifix/agents/runner.py`
- 测试：`tests/test_agent_runner.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_agent_runner.py` 末尾：

```python
async def test_cost_cap_stops_consuming(_ev):
    """越线后停止消费 —— 这是整个成本闸的地基。"""
    closed = []

    async def stream():
        try:
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4)
            yield TextDelta(text="第一段")
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4)
            yield TextDelta(text="第二段")
            yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.4)
            yield TextDelta(text="第三段")
        finally:
            closed.append(True)

    out = await consume(stream(), cost_cap=0.5)
    assert out.cost_capped is True
    assert abs(out.cost_usd - 0.8) < 1e-9, "第二次 usage 越线，第三次不该发生"
    assert out.text == "第一段", "越线后的文本不该再被消费"
    assert closed == [True], "必须显式 aclose，不能把生成器留给 GC"


async def test_cost_cap_overshoot_is_one_model_call():
    """契约：越线后不再发起新调用，而不是「不超过一分钱」。

    断言的是「多花了正好一次调用」——直接断言 cost <= cap 会是假的，
    而写一个假的断言比不写更糟。
    """
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49)
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49)
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.49)

    out = await consume(stream(), cost_cap=0.5)
    assert abs(out.cost_usd - 0.98) < 1e-9      # 0.49 未越线，0.98 越线即停
    assert out.cost_capped is True


async def test_no_cap_consumes_everything():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=5.0)
        yield TextDelta(text="尾巴")

    out = await consume(stream())
    assert out.cost_capped is False
    assert out.text == "尾巴"


async def test_cap_not_reached_leaves_flag_false():
    async def stream():
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=0.1)
        yield TextDelta(text="没超")

    out = await consume(stream(), cost_cap=0.5)
    assert out.cost_capped is False
    assert abs(out.cost_usd - 0.1) < 1e-9
    assert out.text == "没超"


async def test_cost_cap_keeps_text_already_received():
    """熔断不该丢掉已经拿到手的输出 —— detect 可能已经吐完 JSON 了。"""
    async def stream():
        yield TextDelta(text='{"suspect_file": "a.py"}')
        yield ModelUsage(usage=Usage(10, 5, 15), cost_usd=9.9)
        yield TextDelta(text="后面的")

    out = await consume(stream(), cost_cap=0.5)
    assert out.text == '{"suspect_file": "a.py"}'
    assert out.error is None, "主动截断不是运行出错，不能污染 error"
```

顶部导入补上（若尚未导入）：

```python
from harness.events import ModelUsage, TextDelta
from harness.usage import Usage
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_agent_runner.py -q -k "cost_cap or no_cap"
```

预期：FAIL，`TypeError: consume() got an unexpected keyword argument 'cost_cap'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/agents/runner.py` 整个替换为：

```python
"""把 AgentLoop 的异步事件流收敛成一个供 LangGraph 节点使用的结果对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from harness.events import Event, ModelUsage, RunError, TextDelta


@dataclass
class AgentOutcome:
    text: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    events: list[Event] = field(default_factory=list)
    # 成本上限触发，事件流被我们**主动**截断。刻意不复用 error：
    # detect_node 用 outcome.ok 决定要不要解析诊断，而模型很可能在被截断前
    # 就已经吐完了 JSON —— 把主动截断混进 error 会白白丢掉一个可用的诊断。
    cost_capped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


async def consume(stream: AsyncIterator[Event],
                  cost_cap: float | None = None) -> AgentOutcome:
    """消费整条事件流。保留全部事件供 trace 使用。

    cost_cap：累计成本越过该值即停止消费并关闭生成器。

    契约是「越线之后不再发起新的模型调用」，不是「绝不超过一分钱」——
    成本只有在调用返回后才知道（ModelUsage 到达的那一刻），所以越线时
    那一次调用必然已经花掉。超支上界因此是可陈述的：一次模型调用。
    """
    parts: list[str] = []
    out = AgentOutcome()
    async for ev in stream:
        out.events.append(ev)
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ModelUsage):
            out.tokens += ev.usage.total_tokens
            out.cost_usd += ev.cost_usd or 0.0
            if cost_cap is not None and out.cost_usd >= cost_cap:
                out.cost_capped = True
                break
        elif isinstance(ev, RunError):
            out.error = ev.error
    if out.cost_capped:
        # 必须显式关闭：只 break 会把异步生成器留给 GC，而框架在其中用
        # ExitStack 还原「打转纠偏」的采样升温 —— 清理时机不确定，就可能
        # 把升温漏给下一次调用。getattr 是为了兼容不提供 aclose 的迭代器。
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            await closer()
    out.text = "".join(parts)
    return out
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_agent_runner.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS（基线 253 + 本任务新增）。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/agents/runner.py tests/test_agent_runner.py
git commit -m "feat(runner): consume() 按真实成本熔断

契约是「越线之后不再发起新的模型调用」，不是「绝不超过一分钱」——
成本只有在调用返回后才知道，越线时那一次必然已经花掉。超支上界
可陈述：一次模型调用。含糊成硬上限就是又一个静默假安全。

cost_capped 独立成字段而不复用 error：detect 用 outcome.ok 决定
要不要解析诊断，而模型可能在被截断前就已吐完 JSON。

必须显式 aclose：只 break 会把生成器留给 GC，框架在其中用 ExitStack
还原打转纠偏的采样升温，清理时机不确定就会漏给下一次调用。"
```

---

### 任务 2：`RunBudget` 的美元动态分配

**文件：**
- 修改：`src/aifix/budget.py`
- 测试：`tests/test_budget.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_budget.py` 末尾：

```python
def test_usd_allocates_by_remaining_failures():
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    assert abs(b.usd_for_failure(remaining_failures=4) - 0.5) < 1e-9
    b.charge(tokens=0, usd=0.5)
    assert abs(b.usd_for_failure(remaining_failures=3) - 0.5) < 1e-9


def test_usd_saved_flows_to_later_failures():
    """与 token 同形状：前面省下的自动流给后面难的。"""
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    b.charge(tokens=0, usd=0.1)
    assert abs(b.usd_for_failure(remaining_failures=1) - 1.9) < 1e-9


def test_usd_has_no_floor():
    """token 有下限（再紧也要给一次有意义尝试），美元没有 —— 给下限
    就等于让闸失效，而那正是要挡住的事。"""
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    b.charge(tokens=0, usd=2.0)
    assert b.usd_for_failure(remaining_failures=1) == 0.0


def test_usd_zero_remaining_failures_returns_all():
    b = RunBudget(total_tokens=100_000, total_usd=2.0, total_seconds=600)
    assert abs(b.usd_for_failure(remaining_failures=0) - 2.0) < 1e-9


def test_remaining_usd_never_negative():
    b = RunBudget(total_tokens=100_000, total_usd=1.0, total_seconds=600)
    b.charge(tokens=0, usd=1.5)
    assert b.remaining_usd() == 0.0
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_budget.py -q -k usd
```

预期：FAIL，`AttributeError: 'RunBudget' object has no attribute 'usd_for_failure'`。

- [ ] **步骤 3：编写实现**

在 `src/aifix/budget.py` 的 `for_failure` 之后插入：

```python
    def remaining_usd(self) -> float:
        return max(self._total_usd - self.spent_usd, 0.0)

    def usd_for_failure(self, remaining_failures: int) -> float:
        """分给下一个 failure 的美元额度。

        与 token 的 for_failure 同形状：动态分配而非固定切分，前面省下的
        自动流给后面难的。

        但**刻意没有下限**。token 那边设 FLOOR_TOKENS 是「再紧也要给一次
        有意义尝试的余地」；美元这边额度耗尽时若还给一个下限，闸就失效了
        —— 而「额度耗尽还在花」正是这个设计要挡住的事。
        """
        left = self.remaining_usd()
        if remaining_failures <= 0:
            return left
        return left / remaining_failures
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_budget.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：Commit**

```bash
git add src/aifix/budget.py tests/test_budget.py
git commit -m "feat(budget): 美元的单 failure 动态分配

与 token 的 for_failure 同形状，但刻意没有下限：token 的 FLOOR_TOKENS
是「再紧也要给一次有意义尝试的余地」，美元给下限等于让闸失效，
而那正是要挡住的事。"
```

---

### 任务 3：把成本闸接进 `fix_node`

**文件：**
- 修改：`src/aifix/nodes/fix.py`、`src/aifix/graph.py`、`src/aifix/cli.py`
- 测试：`tests/test_nodes_fix_guards.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_nodes_fix_guards.py` 末尾：

```python
async def test_fix_stops_when_failure_usd_budget_exhausted(buggy_repo):
    """单 failure 的美元额度用完 —— 不再发起新的模型调用。"""
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, price_map={"gpt-4o-mini": [1000.0, 1000.0]})
        st["failure_usd_budget"] = 0.001      # 一次调用就必然越线
        client = _Scripted([_text("我想想"),
                            _tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("改好了")])
        out = await fix_node(st, client=client)
        assert client.calls == 1, "越线后不该再发起第二次调用"
        assert out["cost_capped"] is True


async def test_fix_without_usd_budget_runs_normally(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt)
        client = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                            _text("已修复")])
        out = await fix_node(st, client=client)
        assert out["cost_capped"] is False
        assert out["diff_lines"] == 2
```

同时把 `tests/test_nodes_fix_guards.py` 里的 `_state` 辅助改为支持传 `price_map`（它当前是 `AifixConfig(**over)`，已经支持，无需改动——确认即可）。

**注意**：`_Scripted` 本身不产生成本，成本来自框架的 `effective_cost(usage, model_name, price_map)`。所以测试要靠 `price_map` 让成本非零，而**价表的键必须是模型名** —— `HarnessConfig().model` 默认是 `"gpt-4o-mini"`，写成空串会匹配不到、成本恒为 0，于是这条测试会在什么都没测到的情况下「通过」。

实测：`effective_cost(Usage(10, 5, 15), "gpt-4o-mini", {"gpt-4o-mini": [1000.0, 1000.0]})` = `15.0`（输入 10/1k×1000 + 输出 5/1k×1000）。

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q -k usd_budget
```

预期：FAIL，`KeyError: 'cost_capped'`。

- [ ] **步骤 3：`AifixState` 补字段**

在 `src/aifix/graph.py` 的 `AifixState` 中，`failure_token_budget` 之后插入：

```python
    failure_usd_budget: float
    cost_capped: bool
```

并在 `new_state` 的返回值中补：

```python
        failure_usd_budget=0.0, cost_capped=False,
```

- [ ] **步骤 4：`fix_node` 接成本闸**

把 `src/aifix/nodes/fix.py` 中

```python
    remaining = state.get("failure_token_budget") or max(
        cfg.budget_tokens - state["spent_tokens"], 10_000)
```

之后补上：

```python
    # 本轮 failure 分到的美元额度。0 / 缺席表示不设美元闸（退回 token 闸）。
    usd_alloc = state.get("failure_usd_budget") or None
    cost_capped = False
```

把循环里的

```python
            outcome = await consume(loop.run(messages=list(messages)))
```

改为

```python
            # 额度是**整个 failure** 的，不是每轮的：守卫重试是同一次修复
            # 尝试的延续，各轮分别给一份额度等于把上限悄悄放大数倍。
            round_cap = None if usd_alloc is None else usd_alloc - cost
            if round_cap is not None and round_cap <= 0:
                cost_capped = True
                break
            outcome = await consume(loop.run(messages=list(messages)),
                                    cost_cap=round_cap)
```

在 `cost += outcome.cost_usd` 之后补：

```python
            if outcome.cost_capped:
                cost_capped = True
```

并在守卫判定之后、拼接反馈消息之前插入提前退出：

```python
            if cost_capped:
                break
```

最后在返回字典里补：

```python
        "cost_capped": cost_capped,
```

- [ ] **步骤 5：`cli.run_once` 分配美元额度**

在 `src/aifix/cli.py` 中，`state["failure_token_budget"] = ...` 之后补：

```python
                state["failure_usd_budget"] = budget.usd_for_failure(
                    len(state["queue"]) + 1)
```

- [ ] **步骤 6：运行测试验证通过**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q
```

预期：全部 PASS。

- [ ] **步骤 7：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 8：Commit**

```bash
git add src/aifix/nodes/fix.py src/aifix/graph.py src/aifix/cli.py \
        tests/test_nodes_fix_guards.py
git commit -m "feat(nodes): fix 节点接入单 failure 的美元闸

额度是整个 failure 的而非每轮的 —— 守卫重试是同一次修复尝试的延续，
各轮分别给一份额度等于把上限悄悄放大数倍。

只接 fix_node 不接 detect_node：detect 是单步无工具、已被
detector_max_tokens 硬限住的一次调用，钱不在那里烧。"
```

---

# 阶段 2：守卫连续触发即放弃

### 任务 4：同一守卫连续 N 次即放弃该 failure

**文件：**
- 修改：`src/aifix/config.py`、`src/aifix/nodes/fix.py`
- 测试：`tests/test_nodes_fix_guards.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_nodes_fix_guards.py` 末尾：

```python
async def test_same_guard_twice_gives_up(buggy_repo):
    """连续两次空 diff = 同一堵墙撞两回，别再撞第三次。

    实测两个真实模型都在这里各烧了 51~52 万 token 却一个字没改。
    """
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=5, guard_giveup_limit=2)
        client = _Scripted([_text("已修复")])       # 永远不改任何文件
        out = await fix_node(st, client=client)
        assert client.calls == 2, "第二次空 diff 即放弃，不该跑满 6 轮"
        assert out["abort_reason"] == "empty_diff_giveup"
        assert out["guard_hits"] == ["empty_diff", "empty_diff"]


async def test_alternating_guards_do_not_give_up(buggy_repo):
    """交替触发说明模型在换思路，值得再给一次机会。"""
    big = "".join(f"+line{i}\n" for i in range(400))
    huge = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,400 @@\n"
            "-def add(a, b):\n"
            "-    return a - b        # bug: 应为 a + b\n" + big)
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, max_diff_lines=50,
                    fix_guard_retries=3, guard_giveup_limit=2)
        client = _Scripted([
            _text("先不改"),                                    # empty_diff
            _tool("apply_patch", json.dumps({"diff": huge})),   # huge_diff
            _text("重写完毕"),
            _tool("apply_patch", json.dumps({"diff": _PATCH})),
            _text("这次只改一行"),
        ])
        out = await fix_node(st, client=client)
        assert out["guard_hits"] == ["empty_diff", "huge_diff"]
        assert out["abort_reason"] is None, "交替触发不该提前放弃"
        assert out["diff_lines"] == 2


async def test_giveup_limit_one_gives_up_immediately(buggy_repo):
    with Worktree(buggy_repo, run_id="r1") as wt:
        st = _state(buggy_repo, wt, fix_guard_retries=5, guard_giveup_limit=1)
        client = _Scripted([_text("已修复")])
        out = await fix_node(st, client=client)
        assert client.calls == 1
        assert out["abort_reason"] == "empty_diff_giveup"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q -k "give_up or giveup or alternating"
```

预期：FAIL，断言 `client.calls == 2` 不成立（当前会跑满 `fix_guard_retries + 1` 轮）。

- [ ] **步骤 3：给 `AifixConfig` 加字段**

在 `src/aifix/config.py` 的 `fix_guard_retries` 之后插入：

```python
    # 同一条守卫连续触发多少次即放弃该 failure。用「同一条」而不是「任意
    # 守卫」：交替触发（空 diff → 巨型 diff）说明模型在换思路，值得再给一次；
    # 连续两次空 diff 是同一堵墙撞两回。实测两个真实模型都在这里各烧了
    # 51~52 万 token 却一个字没改。
    guard_giveup_limit: int = 2
```

- [ ] **步骤 4：`fix_node` 实现放弃规则**

在 `src/aifix/nodes/fix.py` 循环之前补：

```python
    last_guard: str | None = None
    guard_repeats = 0
```

把循环里守卫判定的两个分支改为只决定 `kind` 与 `feedback`，然后统一处理。即把

```python
            if lines == 0:
                guard_hits.append("empty_diff")
                abort_reason = "empty_diff"
                feedback = _EMPTY_FEEDBACK
            elif lines > cfg.max_diff_lines:
                guard_hits.append("huge_diff")
                abort_reason = "huge_diff"
                feedback = _HUGE_FEEDBACK.format(
                    lines=lines, limit=cfg.max_diff_lines)
                await _rollback(sandbox)
                touched.clear()
                lines = 0
            else:
                abort_reason = None
                break
```

替换为

```python
            if lines == 0:
                kind, feedback = "empty_diff", _EMPTY_FEEDBACK
            elif lines > cfg.max_diff_lines:
                kind = "huge_diff"
                feedback = _HUGE_FEEDBACK.format(
                    lines=lines, limit=cfg.max_diff_lines)
                await _rollback(sandbox)
                touched.clear()
                # 已回滚，工作区确实没有改动了。不清零的话，守卫用尽时记进
                # trace 的会是回滚前的陈旧值 —— 观测数据撒谎比没有观测更糟。
                lines = 0
            else:
                abort_reason = None
                break

            guard_hits.append(kind)
            abort_reason = kind
            guard_repeats = guard_repeats + 1 if kind == last_guard else 1
            last_guard = kind
            if guard_repeats >= cfg.guard_giveup_limit:
                # 同一堵墙撞够了。把「钱花完了」变成「出问题了，去看 trace」——
                # 后者信息量大得多，省下的额度还能流给真有希望的 failure。
                abort_reason = f"{kind}_giveup"
                trace.fact("guard_giveup", kind)
                break

            if cost_capped:
                break
```

**注意**：任务 3 曾在守卫判定之后插入过一句 `if cost_capped: break`。上面这个
替换块的末尾**已经包含它**，请确认替换后文件里只剩一处，不要留下两个。

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/test_nodes_fix_guards.py -q
```

预期：全部 PASS。

- [ ] **步骤 6：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add src/aifix/config.py src/aifix/nodes/fix.py \
        tests/test_nodes_fix_guards.py
git commit -m "feat(nodes): 同一守卫连续 N 次即放弃该 failure

实测两个真实模型都以 empty_diff 收场，各烧 51~52 万 token 一个字没改 ——
守卫会把 fix_guard_retries 用满，每轮都是一次完整的 AgentLoop。

用「同一条守卫」而不是「任意守卫」：交替触发说明模型在换思路，
值得再给一次；连续两次空 diff 是同一堵墙撞两回。

abort_reason 记成 <kind>_giveup 并写 trace.fact，理由与 M2 的连续失败
熔断一致：把「钱花完了」变成「出问题了，去看 trace」。"
```

---

# 阶段 3：启动校验

### 任务 5：显式要求美元上限却无价格表时拒绝启动

**文件：**
- 修改：`src/aifix/cli.py`
- 测试：`tests/test_cli_args.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_cli_args.py` 末尾：

```python
def test_explicit_usd_budget_without_price_map_is_refused():
    """设了上限却没价格表 = 一个系统给不了的保证。当场拒绝。"""
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    cfg = AifixConfig(budget_usd=0.6)          # 显式提供
    with pytest.raises(SystemExit) as e:
        require_price_map_for_usd_budget(cfg)
    msg = str(e.value)
    assert "AIFIX_PRICE_MAP" in msg, "报错要说清楚缺什么、怎么配"


def test_default_usd_budget_without_price_map_is_allowed():
    """没显式要求就不打扰 —— 退回 token 闸。"""
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    require_price_map_for_usd_budget(AifixConfig())   # 不抛即通过


def test_explicit_usd_budget_with_price_map_is_allowed():
    from aifix.cli import require_price_map_for_usd_budget
    from aifix.config import AifixConfig

    cfg = AifixConfig(budget_usd=0.6, price_map={"m": [0.001, 0.002]})
    require_price_map_for_usd_budget(cfg)             # 不抛即通过


def test_cli_budget_flag_counts_as_explicit():
    """--budget 走的是 model_copy，也会被 model_fields_set 记住。"""
    from aifix.config import AifixConfig

    cfg = AifixConfig().model_copy(update={"budget_usd": 0.6})
    assert "budget_usd" in cfg.model_fields_set
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_cli_args.py -q -k price_map
```

预期：FAIL，`ImportError: cannot import name 'require_price_map_for_usd_budget'`。

- [ ] **步骤 3：编写实现**

在 `src/aifix/cli.py` 的 `build_parser` 之前插入：

```python
def require_price_map_for_usd_budget(config: AifixConfig,
                                     also_explicit: bool = False) -> None:
    """显式要求了美元上限却没有价格表 —— 当场拒绝。

    also_explicit：给 `eval --budget-total` 用。那个上限不进配置对象
    （它是这一次调用的调度约束，不是项目配置），所以 model_fields_set
    看不见它，得由调用方把「用户确实要求了美元闸」这件事带进来。

    不配价格表时 effective_cost 恒为 0，美元闸永远不会触发：用户设了上限，
    系统欣然接受，然后一分钱不拦。与其给一个假的保证，不如现在就停。

    「显式」由 pydantic 的 model_fields_set 判定，默认值不在其中；CLI 的
    --budget 走 model_copy(update=...)，同样会被记住，所以一处判定管住
    环境变量、构造参数、命令行三条来源。
    """
    explicit = also_explicit or "budget_usd" in config.model_fields_set
    if not explicit or config.price_map:
        return
    raise SystemExit(
        "拒绝启动：设置了美元预算上限，但没有配置价格表，这个上限不会生效。\n"
        "  没有 price_map 时成本恒为 0，闸永远不触发 —— 与其给一个假的保证，"
        "不如现在就停。\n"
        "  修法一：配置价格表（每千 token 的 [输入价, 输出价]）\n"
        "    export AIFIX_PRICE_MAP='{\"deepseek-v4-pro\": [0.003, 0.006]}'\n"
        "  修法二：去掉美元上限，改用 AIFIX_BUDGET_TOKENS 限制 token")
```

**注意**：这条校验放在 CLI 层，**不做成 pydantic 校验器**。库调用方与大量既有测试都会直接构造 `AifixConfig(budget_usd=...)`，做成校验器会把它们全部打断，而它们本就不需要美元闸。

在 `_cmd_run` 的 `config = ...` 之后调用：

```python
    require_price_map_for_usd_budget(config)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_cli_args.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/cli.py tests/test_cli_args.py
git commit -m "feat(cli): 显式要求美元上限却无价格表时拒绝启动

不配价格表时成本恒为 0，美元闸永远不触发 —— 用户设了上限，系统欣然
接受，然后一分钱不拦。与其给一个假的保证，不如当场停。

「显式」由 model_fields_set 判定，默认值不在其中，所以用默认配置的
调用者不受打扰。放在 CLI 层而非 pydantic 校验器：库调用方与大量既有
测试都会直接构造 AifixConfig(budget_usd=...)，它们本就不需要美元闸。"
```

---

# 阶段 4：整批评测的总闸

### 任务 6：`run_suite` 的整批预算

**文件：**
- 修改：`src/aifix/eval/runner.py`
- 测试：`tests/test_eval_runner.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_eval_runner.py` 末尾：

```python
def _bare_task(tid: str) -> Task:
    """只为调度逻辑服务的任务壳 —— 字段不会被 fake 的 run_task 读到。"""
    return Task(task_id=tid, repo="/tmp/x", commit="c", base_commit="b",
                test_files=["tests/t.py"], target_test="tests/t.py::x",
                gold_files=["a.py"])


def _fake_run_task(seen: list, caps: list, cost: float):
    """造一个花固定钱数的 run_task 替身。

    直接控制「一个任务花多少钱」，而不是让完整修复闭环去算 —— 这几条测的
    是**调度逻辑**，不该被闭环的成本波动牵着走，也不该为此跑几分钟测试。
    """
    async def fake(task, config, model, workdir, **kw):
        seen.append(task.task_id)
        caps.append(config.budget_usd)
        return TaskResult(task_id=task.task_id, model=model, locate_hit=True,
                          suspect_file="a.py", verdict="better", attempts=1,
                          tokens=100, cost_usd=cost, violations=0)
    return fake


async def test_suite_budget_stops_dispatching(monkeypatch, tmp_path):
    """整批额度花完就停止派发；已跑的照常，未跑的记成评测故障。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    tasks = [_bare_task(f"t{i}") for i in range(4)]
    rs = await R.run_suite(tasks, AifixConfig(budget_usd=5.0), "假模型",
                           tmp_path, parallel=1, total_usd=2.0)

    assert seen == ["t0", "t1"], "花满 2.0 之后不该再派发"
    assert [r.task_id for r in rs] == ["t0", "t1", "t2", "t3"], "顺序必须保持"
    assert rs[0].error is None and rs[1].error is None
    assert all("未运行" in (r.error or "") for r in rs[2:])


async def test_skipped_tasks_do_not_count_as_failures(monkeypatch, tmp_path):
    """跳过的任务不能进比率分母 —— 否则被测系统替调度决策背锅。"""
    import aifix.eval.runner as R
    from aifix.eval.score import summarize

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    rs = await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                           AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                           parallel=1, total_usd=1.0)
    s = summarize(rs)
    assert s.tasks == 1, "只有真跑过的进分母"
    assert s.errors == 2
    assert s.fix_rate == 1.0, "跳过的不该把成功率拉低"


async def test_per_task_cap_is_clamped_to_remaining(monkeypatch, tmp_path):
    """最后一个任务只拿得到剩下的钱 —— 不能起一个付不起的活。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=1.0))
    await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                      AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                      parallel=1, total_usd=1.5)
    assert caps == [1.5, 0.5], "第一个被整批剩余压到 1.5，第二个只剩 0.5"


async def test_no_suite_budget_leaves_config_untouched(monkeypatch, tmp_path):
    """不给整批上限时全跑，且不改写每任务额度。"""
    import aifix.eval.runner as R

    seen, caps = [], []
    monkeypatch.setattr(R, "run_task", _fake_run_task(seen, caps, cost=99.0))
    rs = await R.run_suite([_bare_task(f"t{i}") for i in range(3)],
                           AifixConfig(budget_usd=5.0), "假模型", tmp_path,
                           parallel=1)
    assert seen == ["t0", "t1", "t2"]
    assert caps == [5.0, 5.0, 5.0], "没有整批闸时不该压低每任务额度"
    assert all(r.error is None for r in rs)
```

顶部导入补上 `Task`（若尚未导入）：

```python
from aifix.eval.task import Task, TaskResult
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_eval_runner.py -q -k suite_budget
```

预期：FAIL，`TypeError: run_suite() got an unexpected keyword argument 'total_usd'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/eval/runner.py` 的 `run_suite` 替换为：

```python
_SKIPPED = "整批预算耗尽，未运行"


def _blank(task_id: str, model: str, error: str) -> TaskResult:
    return TaskResult(task_id=task_id, model=model, locate_hit=False,
                      suspect_file=None, verdict="same", attempts=0,
                      tokens=0, cost_usd=0.0, violations=0, error=error)


async def run_suite(tasks: list[Task], config: AifixConfig, model: str,
                    workdir: Path, parallel: int = 4,
                    detector_client: Any = None,
                    fixer_client: Any = None,
                    on_done=None,
                    total_usd: float | None = None) -> list[TaskResult]:
    """并行跑整个任务集。返回顺序与传入顺序一致。

    total_usd：整批的美元上限。检查发生在**派发之前**，已经在跑的任务
    放它们跑完 —— 它们的结果是有效数据，中途掐掉等于白花已经花掉的钱。
    因此超支上界是「并发数 − 1 个任务」的成本，这一点要在 --help 里写明。
    """
    sem = asyncio.Semaphore(parallel)
    spent = 0.0
    lock = asyncio.Lock()

    async def one(t: Task) -> TaskResult:
        nonlocal spent
        async with sem:
            if total_usd is not None:
                async with lock:
                    left = total_usd - spent
                if left <= 0:
                    # 记成 error 而不是失败的 verdict：这是评测的调度决策，
                    # 不是被测系统的成绩。混进比率分母会让修复成功率凭空
                    # 变低 —— 被测系统替调度背锅。
                    r = _blank(t.task_id, model, _SKIPPED)
                    if on_done:
                        on_done(r)
                    return r
                task_config = config.model_copy(
                    update={"budget_usd": min(config.budget_usd, left)})
            else:
                task_config = config
            try:
                r = await run_task(t, task_config, model, workdir,
                                   detector_client=detector_client,
                                   fixer_client=fixer_client)
            except Exception as e:      # 一个任务炸掉不能带走整个 suite
                r = _blank(t.task_id, model, repr(e))
            async with lock:
                spent += r.cost_usd
            if on_done:
                on_done(r)
            return r

    return list(await asyncio.gather(*(one(t) for t in tasks)))
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_eval_runner.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/eval/runner.py tests/test_eval_runner.py
git commit -m "feat(eval): 整批评测的美元总闸

此前 aifix eval 没有任何预算参数，且每任务各自建预算 —— 默认
budget_usd=2.0 是每任务的，20 个任务一轮上限 \$40，完全无闸。

检查在派发前，已在跑的任务放它们跑完：结果是有效数据，中途掐掉
等于白花已经花掉的钱。超支上界因此是「并发数 - 1 个任务」。

未跑的任务记成 error 而非失败的 verdict —— 那是评测的调度决策，
不是被测系统的成绩，混进分母会让修复成功率凭空变低。"
```

---

### 任务 7：CLI 暴露预算参数并在 `--help` 里写明契约

**文件：**
- 修改：`src/aifix/cli.py`
- 测试：`tests/test_cli_args.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_cli_args.py` 末尾：

```python
def test_eval_budget_flags():
    a = build_parser().parse_args(
        ["eval", "t.jsonl", "--budget-per-task", "0.5", "--budget-total", "5"])
    assert a.budget_per_task == 0.5
    assert a.budget_total == 5.0


def test_eval_budget_flags_default_to_none():
    a = build_parser().parse_args(["eval", "t.jsonl"])
    assert a.budget_per_task is None
    assert a.budget_total is None


def _sub_help(name: str) -> str:
    """取某个子命令的帮助文本。

    argparse 没有公开的取法，只能从 actions 里找 _SubParsersAction；
    按类型找而不是按下标取，子命令增减时不会错位。
    """
    import argparse
    for act in build_parser()._actions:
        if isinstance(act, argparse._SubParsersAction):
            return act.choices[name].format_help()
    raise AssertionError("没有找到子命令解析器")


def test_run_budget_help_states_the_contract():
    """契约必须出现在 --help 里：越线后不再发起新调用，不是不超一分钱。

    用户有权知道这个保证的边界在哪儿，而不是超支之后才发现。
    """
    assert "不再发起新的模型调用" in _sub_help("run")


def test_eval_total_help_states_the_overshoot_bound():
    assert "并发数" in _sub_help("eval"), "超支上界要写进 --help"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/test_cli_args.py -q -k "eval_budget or contract or overshoot"
```

预期：FAIL，`AttributeError: 'Namespace' object has no attribute 'budget_per_task'`。

- [ ] **步骤 3：编写实现**

把 `src/aifix/cli.py` 中 `run` 子命令的 `--budget` 帮助文本改为：

```python
    run.add_argument("--budget", type=float, default=None,
                     help="本次 run 的美元上限。语义是「越线之后不再发起新的"
                          "模型调用」——成本只有在调用返回后才知道，所以最后"
                          "那一次必然已经花掉。需要配置 AIFIX_PRICE_MAP")
```

在 `eval` 子命令中补两个参数：

```python
    ev.add_argument("--budget-per-task", type=float, default=None,
                    help="每个任务的美元上限")
    ev.add_argument("--budget-total", type=float, default=None,
                    help="整批的美元上限。检查发生在派发之前，已在跑的任务会"
                         "跑完，所以最坏情况会超出「并发数 - 1 个任务」的成本")
```

把 `_cmd_eval` 中构造 config 的部分改为：

```python
    config = AifixConfig()
    if args.budget_per_task is not None:
        config = config.model_copy(
            update={"budget_usd": args.budget_per_task})
    require_price_map_for_usd_budget(
        config, also_explicit=args.budget_total is not None)
```

并把 `run_suite(...)` 的调用补上 `total_usd=args.budget_total`。

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/test_cli_args.py -q
```

预期：全部 PASS。

- [ ] **步骤 5：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add src/aifix/cli.py tests/test_cli_args.py
git commit -m "feat(cli): eval 的两级预算参数，并把契约写进 --help

刻意不复用 run 的 --budget 这个名字：run 只有一个 run 不会有歧义，
eval 有两级，一个裸 --budget 会被读成整批总额。eval 今天一个预算
参数都没有，没有向后兼容负担，此刻正是把名字取对的时机。

--help 里写明两件事：美元闸的语义是「越线后不再发起新的模型调用」，
以及整批闸的超支上界是「并发数 - 1 个任务」。用户有权知道保证的
边界在哪儿，而不是事后才发现。"
```

---

# 阶段 5：端到端

### 任务 8：成本闸的端到端验证

**文件：**
- 测试：`tests/test_cost_gate_e2e.py`

- [ ] **步骤 1：编写测试**

创建 `tests/test_cost_gate_e2e.py`：

```python
"""成本闸的端到端：从配置到报告，钱真的被拦住了。"""
import json

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "x", "fix_strategy": "y", "confidence": "high"})

# 价表的键必须是**模型名**，而 HarnessConfig().model 默认是 "gpt-4o-mini"
# （不是空串 —— 写错的话 effective_cost 匹配不到价表、成本恒为 0，
# 成本闸的测试会全部沦为空转而依然「通过」）。
# 单价定得极高：effective_cost = 输入/1k×价 + 输出/1k×价，
# Usage(10,5,15) 配 [1000,1000] 恰好是 10+5=15 美元，一次调用就越线。
_PRICEY = {"gpt-4o-mini": [1000.0, 1000.0]}


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


async def test_usd_budget_stops_the_run(buggy_repo):
    """极小的美元额度 —— fix 不该跑满，报告要如实说没修好。"""
    fixer = _Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")])
    state = await run_once(
        buggy_repo, AifixConfig(budget_usd=0.0001, price_map=_PRICEY),
        run_id="cap1",
        detector_client=_Scripted([_text(_DIAG)]), fixer_client=fixer)
    assert state["results"][0]["verdict"] != "better"
    assert fixer.calls <= 1, "越线后不该再发起新的模型调用"


async def test_generous_budget_lets_it_finish(buggy_repo):
    """对照组：额度足够时行为不变，证明上面那条测的是闸不是别的。"""
    state = await run_once(
        buggy_repo, AifixConfig(budget_usd=1000.0, price_map=_PRICEY),
        run_id="cap2",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch",
                                      json.dumps({"diff": _PATCH})),
                                _text("已修复")]))
    assert state["results"][0]["verdict"] == "better"


async def test_guard_giveup_shows_in_report(buggy_repo):
    """连续空 diff 放弃后，报告里的中止原因要能区分出是哪一条守卫。"""
    state = await run_once(
        buggy_repo, AifixConfig(fix_guard_retries=5, guard_giveup_limit=2),
        run_id="giveup1",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_text("已修复")]))
    assert "empty_diff_giveup" in state["report_md"]
```

- [ ] **步骤 2：运行测试**

```bash
uv run pytest tests/test_cost_gate_e2e.py -q
```

预期：3 passed。若失败，说明前面某个任务的接线没打通——按报错定位，不要在这里加特例。

- [ ] **步骤 3：运行全量验证通过**

```bash
uv run pytest -q
```

预期：全部 PASS。

- [ ] **步骤 4：Commit**

```bash
git add tests/test_cost_gate_e2e.py
git commit -m "test(cost): 成本闸端到端

含对照组：额度足够时行为不变，证明极小额度那条测的是闸本身，
而不是别的什么东西恰好让它失败。"
```

---

## M3b 完成标志

- [ ] `uv run pytest -q` 全绿
- [ ] `aifix run --help` 与 `aifix eval --help` 里都能读到契约与超支上界
- [ ] 显式设 `AIFIX_BUDGET_USD` 而不配价格表 → 启动即拒绝，报错说清怎么修
- [ ] 真实模型跑一次：配好价格表，`aifix run --budget <极小值>`，确认在越线后停止且报告如实
- [ ] 真实模型跑一次：`aifix eval --budget-total <小值>`，确认后续任务被跳过、且跳过的进「评测故障」列而不拉低修复成功率

最后两条需手动执行，且**必须配置 `AIFIX_PRICE_MAP`**——否则按本计划的设计会被启动校验直接拒绝（这本身也是一条验收）。

## 交给后续里程碑

本计划只做 B 组「成本闸」。A 组（规格套利）、C 组（适配层）、D 组（评测规模化）、E 组（诊断工具）见 M3 计划末尾的缺口表。
