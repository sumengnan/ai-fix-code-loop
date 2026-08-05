# 复现测试必须真的执行被测代码 —— 设计规格

**日期**：2026-08-05
**起因**：ai-learning-helper#95 / PR#96，aifix 报「修复 1/1」，而那条复现测试是无效的
**范围**：`reproducer` 的适配器路由 + 一道新的静态闸

---

## 1. 问题陈述

aifix 对 ai-learning-helper#95 产出了这条复现测试：

```python
from pathlib import Path
import pytest

_EMPTY_HINT = Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "EmptyHint.tsx"

def test_suggestions_contains_generate_5_ai_questions():
    """SUGGESTIONS 数组必须包含「生成5道ai题」这条默认对话。"""
    text = _EMPTY_HINT.read_text(encoding="utf-8")
    assert "生成5道ai题" in text
```

它对产品源文件做**字符串 grep**，不执行其中任何一行。把补丁整个撤销、只在文件里留一句
`// TODO: 以后考虑加一条「生成5道ai题」的建议，现在还没加`，这条测试照样通过 ——
它区分不了「实现了」和「明确没实现」。

判定却一路放行，报告写「修复 1/1」。

### 1.1 根因：模型没有第二个选项

`detect_adapter`（`nodes/baseline.py:170`）只返回**一个**适配器，`AIFIX_ADAPTERS`
给的顺序里 pytest 在前，于是每一条 issue 都拿到 pytest —— 包括报 `.tsx` 缺陷的那些。
reproducer 收到的 prompt 是「本项目的测试目录：tests/，新测试文件必须写在其中之一的下面」。

**用 pytest 写一条关于 `.tsx` 的测试，唯一的写法就是把它当文本读。** 那条 grep 不是
模型偷懒，是这个约束下的唯一解。

`baseline.py:185-188` 已经登记过这个窟窿：

> 前后端同仓的工程走这三条路时，仍然只有一套体系能被复现/挖掘到。报另一侧的缺陷时
> 模型会拿错语言写测试，而红检只会说「这条测试没红」——一句指错方向的话。

**实际发生的比这段预言更糟。** 预言假设红检会兜住，而模型找到了一条让 pytest 测试
真的红了又真的绿了的路，红检因此放行。

### 1.2 五道闸逐个为什么哑

| 闸 | 位置 | 原因 |
|---|---|---|
| 字段自洽校验 | `agents/reproducer.py:194` | 只查路径合法 / 自包含 / id 可追溯，不问测试干了什么 |
| 红检 | — | 结构性失明：grep 式测试天生红→绿，红绿信号上与真测试不可区分 |
| 必要性反查 | `checks/necessity.py:271` | `len(units) <= 1` 整层跳过，补丁只有一个 hunk |
| 变形复跑 | `checks/metamorphic.py:81` | 只扰动「≥2 元素的全常量 list」，测试里没有 list，`checked == 0` |
| 硬编码判断 | `checks/signals.py:254` | 查新增 `if` 里的测试字面量；补丁加的是数组元素，且 `.tsx` parse 不了 |

共同点：除红检外全是 Python-AST 的，而红检对这一类结构性失明。

---

## 2. 目标与非目标

**目标**

1. reproducer 在多适配器仓库里**自己选**该写哪一套体系的测试
2. 一道静态闸拦住「复现测试根本不接触本项目代码」
3. 被拦住时退回重写，且**退回的代价有界**

**非目标**

- 不做 coverage 度量。判据保持纯静态、零重跑、零新依赖
- 不给 TypeScript / Java 写解析。新闸是 Python-only，与 `signals.py:220` 同一条既有约定
- 不改判定的三态语义。这道闸作用在复现阶段，在 fixer 花钱之前

---

## 3. 判据：复现测试必须 import 本项目的模块

收集 `test_code` 里所有 `Import` / `ImportFrom` 的**顶层根名**，至少要有一个能解析到
本仓库的顶层模块。

**「本仓库的顶层模块」的判据**：仓库根下存在同名目录，或同名 `.py` 文件；`src/` 布局
再看一层 `src/<name>`。测试目录本身排除在外。

**不要求 `__init__.py`**：PEP 420 命名空间包是真实存在的形态 —— ai-learning-helper 的
`agents/`、`mcp/`、`src/` 都没有 `__init__.py`，要求它会让判据在这个仓库上恒不响。

#96 那条测试的根名是 `{pathlib, pytest}`，与仓库顶层名一个都不交 —— 当场抓住。

### 3.1 失效方向

漏报是安全的，误报是贵的，判据要朝漏报那一侧倒 —— 与 `_missing_names`
（`reproducer.py:148`）同一条理由。

- **漏报**：仓库里恰好有个顶层目录叫 `json`，于是 `import json` 被算成本项目模块。
  这条测试逃过检查，与今天的行为一样，没有变糟。
- **误报**：一条只用 `subprocess` 跑 CLI 的合法测试确实不 import 本项目模块，会被误伤。
  这是真实存在的形态，代价由 §4 的「只退回一次」封顶。

---

## 4. 处置：退回重写，只退一次

被判据抓到时回滚这条复现测试，带着原因重跑一次 reproducer。重写提示里明确写出
被拒的理由和那条规则。

**这是一条新通路，不是复用 `_incoherence`。** 现有的字段不自洽走
`parse_reproduction_ex` → `(None, why)` → handle 的 no_repro 回帖，**不重试**。
这道闸要的是「回滚 + 带理由重跑一次 + 计数」，那套机器今天不存在，得新建。

放在 `_incoherence` 之外还有第二个理由：`_incoherence` 回答的是「模型的输出自不自洽」
（纯格式问题，重跑多半还是错），而这道闸回答的是「这条测试有没有意义」——
一个带着理由的重跑对它是有胜算的。两者混在一个返回值里，回帖会把后者说成
「输出不合约定的格式」，那是一句假话。

**只退一次。** 第二次仍不过就**放行**，并在报告里出声：「这条复现测试没有 import
本项目任何模块，它可能没有真的执行被测代码」。

理由是误报的成本必须封顶。一条走 subprocess 的合法测试重写多少次都过不了这道闸 ——
无限退回会把一次本来能成的 run 拖死在一个启发式判据上。判定面不该被一个启发式
永久占据，这与 `necessity.py:12`「这一层不改变判定」是同一条克制。

**这个形状仓库里已有先例**：`metamorphic_diverged` 在 `nodes/report.py:22` 上面
写着「只有在 attempt 用尽、补丁仍被交付时才会出现（还有额度就退回重写了）」——
先退回重写，退不动就交付并在报告里出声。新闸沿用同一条，只是把额度定为 1。

报告里的文案登记进 `nodes/report.py` 的 `_SIGNAL_CN`。

---

## 5. reproducer 自选适配器

### 5.1 改动

- `detect_adapters()` 的**全部**结果进 prompt，每个给 `name` / `test_dirs()` /
  `example_test_id()`
- `Reproduction` 加字段 `harness: str`（适配器名）
- `_incoherence` 改用**选中适配器**的 `is_test_path` 校验 `test_file`，并校验
  `harness` 落在候选集内
- `issue/handle.py:235` 的 `detect_adapter()` 单数调用改为传全部；reproduce 把选中的
  适配器返回给下游 `red_check` / `write_reproduction`

### 5.2 向后兼容

只有一个适配器时 `harness` 可省，取那一个 —— 今天绝大多数仓库是这个形状，它们的
prompt 和行为不应有任何变化。

### 5.3 错误处理

| 情况 | 处置 |
|---|---|
| `harness` 缺失 + 单适配器 | 用那一个 |
| `harness` 缺失 + 多适配器 | 走现有「缺字段」通路（`_incoherence`） |
| `harness` 不在候选集 | `_incoherence` 打回，理由指名候选集 |
| 重写后 import 检查仍不过 | 放行 + 报告出声（§4） |

`_path_is_safe` 必须继续与写入守卫共用同一个谓词。选中适配器之后这个不变式要重新
成立一次：校验用的 `is_test_path` 和随后守卫用的，必须来自**同一个**适配器实例，
否则就有一条缝 —— 校验说它是测试、守卫说它不是，fixer 于是能改掉自己的判卷标准。

---

## 6. 已知盲区

**新闸是 Python-only。** 判据是 `ast`，选了 vitest 之后它不响 —— 一条
`readFileSync(...)` + grep 的 TS 测试照样能溜过去。

按 `Metamorphic`（`checks/metamorphic.py:36`）那套三态约定记成「**没查**」，
而不是「查过没问题」。两者在结果上长得一样，只有分开记，读报告的人才知道
这份结论是不完整的。

补它要写 TS 解析，不在本规格内。

---

## 7. 测试策略

- **判据单测**：`import` 根名收集与仓库顶层名匹配的真/假样本。**把 #96 那条测试的
  原文作为回归样本钉进去** —— 它是这道闸存在的理由，将来任何改动让它重新通过，
  测试就该红
- **命名空间包**：无 `__init__.py` 的顶层目录必须被认作本项目模块（`agents/` 形态）
- **失效方向**：仓库有同名顶层目录时 `import json` 不报（漏报是设计，不是 bug）
- **多适配器 `_incoherence`**：`harness` 缺失 / 不在候选集 / 与 `test_file` 不匹配
- **单适配器向后兼容**：不给 `harness` 时行为与改动前逐字相同
- **只退一次**：第二次仍不过时放行，且报告里那一节确实出现

---

## 8. 不在本规格内

- coverage 度量与任何需要重跑的判据
- TypeScript / Java 的测试源码解析
- 让 reproducer 一次写多套体系的测试
- `AIFIX_ADAPTERS` 的语义变更（它仍然是人给的顺序，只是不再是唯一的裁决者）
