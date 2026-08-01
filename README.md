# aifix

[![PyPI](https://img.shields.io/pypi/v/aifix-code.svg)](https://pypi.org/project/aifix-code/)
[![Python](https://img.shields.io/pypi/pyversions/aifix-code.svg)](https://pypi.org/project/aifix-code/)
[![License](https://img.shields.io/pypi/l/aifix-code.svg)](https://github.com/sumengnan/aifix-code/blob/main/LICENSE)
[![Tests](https://github.com/sumengnan/aifix-code/actions/workflows/tests.yml/badge.svg)](https://github.com/sumengnan/aifix-code/actions/workflows/tests.yml)

**红着的测试进去，一条验证过的补丁分支出来。**

aifix 是一个测试失败驱动的自我修复循环：它跑一遍你的测试，把失败的用例一个个
排队，让模型去定位、去改，然后**用测试本身**判断改得对不对。改对了就提交到一条
独立分支上，改错了当场回滚。人只在最后做一个决定：这个 merge 要不要按下去。

```
你的仓库（红着 3 个用例）
        │
        ▼
   aifix run .
        │
        ▼
分支 aifix/a1b2c3d4（多了 2 个提交）+ 一份报告
        │
        ▼
   你看一眼 diff，git merge
```

---

## 目录

- [它到底做什么](#它到底做什么)
- [三条不肯让步的原则](#三条不肯让步的原则)
- [装上并跑起来](#装上并跑起来)
- [一次 run 里发生了什么](#一次-run-里发生了什么)
- [命令速查](#命令速查)
- [issue 驱动：一条评论换一个 PR](#issue-驱动一条评论换一个-pr)
- [它到底行不行：评测读数](#它到底行不行评测读数)
- [跑不起来时先看这里](#跑不起来时先看这里)
- [更多文档](#更多文档)

---

## 它到底做什么

给它一个 git 仓库，它会：

1. **先跑一遍全量测试**，看清有几个红的（这一步没有任何模型参与）；
2. 对每个红的用例，让一个**诊断模型**读 traceback 和相关源码，猜缺陷在哪；
3. 让一个**修复模型**带着工具（读文件、搜代码、改代码、跑目标用例）去改；
4. **再跑一遍全量测试**，用改前改后的失败集合对比出一个判定：
   - **修好了**（目标用例转绿，且没有任何新的红）→ 提交到交付分支
   - **没改善**（目标还红着）→ 回滚，换个思路再试，最多 3 轮
   - **引入回归**（冒出了新的红）→ 立刻回滚
5. 跑完给一份 Markdown 报告：修好了几个、花了多少钱、哪些补丁值得多看一眼。

**全程不碰你的主工作区。** 所有改动发生在一个 `git worktree` 里，交付物是一条
新分支（`aifix/<run_id>`），要不要合并完全是你的事。

支持两种项目：**pytest**（Python）和 **Maven**（Java）。

---

## 三条不肯让步的原则

这三条决定了这个项目所有别扭的地方，值得先读：

### 1. 只有零 LLM 的确定性代码有资格说「修好了」

判定「这个补丁行不行」的是 `verify.compare()` —— 二十行 Python，比较两次测试
结果的失败集合。模型说什么都不算数。

只要出现**任何**新的失败，哪怕目标用例真的修好了，一律判「引入回归」并回滚。
这比「净改善」保守得多，但这正是敢在真实仓库上跑它的前提。

### 2. 测试是判卷标准，所以模型碰不到测试文件

修复模型的工具面是**白名单**，只有 8 个工具，没有 shell、没有网络。而所有能写
文件的工具都过同一道守卫：写到测试目录下 → 硬拒绝。

理由很直白：让测试通过的唯一正确方式是改源码。允许模型改测试，等于允许它改判
卷标准 —— 而它是真的会去试的（评测里出现过：模型把 `add` 改成有状态函数，去
满足一个自相矛盾的断言，顺手删掉了没有测试覆盖的 `mul`）。

### 3. 数字不能撒谎

这个项目为「假的 `$0.00`」栽过三次，所以现在到处都是这类处理：

- 花了 token 却算出 0 元 → 显示「未知（未配置 AIFIX_PRICE_MAP）」，不显示 `$0.00`
- 一批 run 里有几次取不到修复数 → 报告写「修复 ≥N（不完整：3 次里有 2 次取不到）」，不直接求和
- 测试进程没跑成 → 中止并退出码非 0，绝不说「你的仓库全绿」
- baseline 里全是「整个测试文件导入失败」→ 判定为**环境故障**并中止，不当成待修的 bug 去烧钱

---

## 装上并跑起来

### 环境要求

- Python ≥ 3.11
- git
- 目标项目自己能跑起来的测试环境（详见下面那条「最常见的坑」）

### 安装

```bash
pip install aifix-code        # 或者：uv tool install aifix-code
```

装完之后命令叫 **`aifix`**（不是 `aifix-code`）：

```bash
aifix --help
```

> **发行名与命令名不同不是笔误。** `aifix` 这个名字在 PyPI 上已被另一个无关的包占用，
> 拿不到；而命令、issue 上的 `/aifix`、产物目录 `.aifix/`、全部文档都用 `aifix` 这个
> 词，改它的代价比换一个发行名大得多。与 `scikit-learn` → `sklearn` 同一个形状。

**推荐用 `uv tool install`**：aifix 自己依赖 langgraph、pydantic、openai，装进你项目的
环境里可能起冲突 —— 而它本来就是个独立跑的命令行工具，不需要和被修的项目共用一个环境。
（跑测试用哪个解释器是**另一个**旋钮，见下面那条「最常见的坑」。）

升级：

```bash
pip install --upgrade aifix-code      # uv tool upgrade aifix-code
```

**要改 aifix 自己的代码**，从源码装：

```bash
git clone https://github.com/sumengnan/aifix-code.git
cd aifix-code
uv sync
uv run aifix --help          # 源码树里要带 uv run 前缀
```

> 下文所有例子写的都是 `aifix ...`。如果你是从源码跑的，前面加 `uv run`。

### 配模型

两条模型路由，可以是同一个端点，也可以是两家不同的供应商（诊断挑便宜的、修复挑强的）：

```bash
export AIFIX_FIXER__BASE_URL="https://your-endpoint/v1"
export AIFIX_FIXER__API_KEY="sk-..."
export AIFIX_FIXER__MODEL="qwen3-coder-flash"

export AIFIX_DETECTOR__BASE_URL="https://your-endpoint/v1"
export AIFIX_DETECTOR__API_KEY="sk-..."
export AIFIX_DETECTOR__MODEL="qwen3-coder-flash"

# 价格表：{模型名: [输入价/千token, 输出价/千token]}
# 不配的话成本算出来恒为 0，美元预算闸永远不会触发
export AIFIX_PRICE_MAP='{"qwen3-coder-flash": [0.0003, 0.0012]}'
```

> **注意**：两级下划线 `__` 是嵌套配置的分隔符，不是笔误。

### 第一次跑

先空跑一次，看看有多少活、不花一分钱：

```bash
aifix run /path/to/your/repo --dry-run
```

真跑：

```bash
aifix run /path/to/your/repo --budget 2.0
```

跑完屏幕上会有一份报告（进度走 stderr，报告走 stdout，所以
`aifix run . > report.md` 存出来的文件是干净的）：

```markdown
# aifix run a1b2c3d4

- 适配器：pytest
- 分支：`aifix/a1b2c3d4`
- 修复：**2 / 3**
- 成本：$0.14（238,070 tokens）

| 测试用例 | 结果 | 尝试次数 | 中止原因 |
|---|---|---|---|
| `tests/test_cart.py::test_total` | 已修复 | 1 | — |
| `tests/test_cart.py::test_empty` | 已修复 | 2 | — |
| `tests/test_api.py::test_auth` | 未改善 | 3 | max_attempts |

合并：`git merge aifix/a1b2c3d4`
```

### 最常见的坑：用哪个 Python 跑测试

**目标项目的测试依赖装在它自己的环境里，不在 aifix 的环境里。** aifix 会自动
探测源仓库下的 `.venv/bin/python` 或 `venv/bin/python`，探不到才退回自己的解释器。

实测过一次对照：拿 aifix 的 venv 去跑另一个项目的测试 → 11 个 collection error，
一个用例都没跑到；换成它自己的 `.venv` → 673 passed。

探测不到就显式配：

```bash
export AIFIX_TEST_PYTHON=/path/to/目标项目/.venv/bin/python
```

还有一个更隐蔽的陷阱：目标项目如果把自己**可编辑安装**（`pip install -e .`）
进了那个解释器，`import <目标包>` 可能解析到**源仓库**而不是打了补丁的 worktree
—— 测试照跑照绿，而验证的是没打补丁的代码，结论是假的。aifix 会在 baseline 之前
探测并往 stderr 出声，但那是提醒不是保证。最可靠的自保是在目标项目的 pytest 配置
里设 `pythonpath`：

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
```

---

## 一次 run 里发生了什么

```
preflight   探测适配器 / 检查工作区干净 / 校验测试解释器          零 LLM
    ↓       （任一不满足 → 中止，退出码 1）
探针        往模型端点发一次最小调用，确认连得上                 一次极小调用
    ↓       （`--dry-run` 时跳过）
建 worktree 从 HEAD 开一个隔离工作区 + 分支 aifix/<run_id>
    ↓
baseline    跑全量测试 → JUnit XML → 失败集合                    零 LLM
    ↓       （全绿 → 直接出报告，「没活干」）
┌── detect  traceback + 真实源码片段 → 结构化诊断 JSON           单步、无工具
│      ↓
│   fix     读码 / 搜索 / 改代码 / 跑目标用例                     多步、8 个工具
│      ↓    （改动为空或过大 → 带反馈重试，不计入 attempt）
│   verify  再跑全量 → 抖动过滤 → 三态判定 → commit 或 rollback   零 LLM
│      ↓
└──── 没修好且还有轮次 → 回到 detect；修好了或用尽 → 下一个 failure
    ↓
report      渲染 Markdown 报告，落盘到 .aifix/runs/<run_id>/
```

几个值得知道的细节：

- **全量测试很贵，所以 baseline 整个 run 只跑一次**，之后每轮 verify 各跑一次。
- **抖动过滤**：verify 发现新的失败时，会只重跑那几个用例确认一次。重跑还红的
  才算真回归；重跑绿了的算抖动，从判定里剔除。成本近似为零，却挡掉了绝大部分
  「把一个本来正确的补丁滚掉」—— 那是这个系统最昂贵的错误。
- **连续失败熔断**：连着 3 个 failure 一个都没修好就整体中止。那多半不是「这些
  bug 恰好都难」，而是环境坏了 / 端点不通 / 今天这个模型不行。继续跑只是匀速烧钱。
- **信息不全时它会停下来问你**（`ask_user`），把问题和 2–4 个选项写进报告，
  用 `aifix answer <编号>` 带着答案重跑。

详见 [docs/architecture.md](https://github.com/sumengnan/aifix-code/blob/main/docs/architecture.md)。

---

## 命令速查

| 命令 | 干什么 |
|---|---|
| `aifix run [repo]` | 修复当前仓库的失败测试 —— 主命令 |
| `aifix answer <编号> [repo]` | 回答上次 run 提出的问题，带着答案重新跑 |
| `aifix reproduce [repo] --issue-text FILE` | 把一段缺陷报告译成一条复现测试（只到复现为止，不改产品代码） |
| `aifix mine [repo]` | 从 git history 挖评测任务集（自带 ground truth） |
| `aifix mutate [repo]` | 人造变异生成冒烟任务集 |
| `aifix eval <tasks.jsonl>` | 在任务集上跑评测 |
| `aifix eval-report <results...>` | 把若干轮结果渲染成跨模型对比表 |
| `aifix replay <run_id>` | 回放一次 run 的逐步复盘 |
| `aifix ingest` | 把各次 run 的事实灌进 `.aifix/trajectory.db` |
| `aifix stats` | 跨 run 汇总：适配器、守卫触发、可疑信号 |
| `aifix issue handle` | 处理一次 GitHub `issue_comment` 事件（在 Actions 里被调起） |

每个命令的完整参数、退出码语义见 [docs/cli.md](https://github.com/sumengnan/aifix-code/blob/main/docs/cli.md)；
所有环境变量见 [docs/configuration.md](https://github.com/sumengnan/aifix-code/blob/main/docs/configuration.md)。

### 退出码

只有一类情况退非 0：**这次 run 没跑成**（崩溃、baseline 全是收集错误、模型端点
不通、preflight 拦下）。

**预算耗尽不在此列** —— 那是正常收场：活干到钱花完为止，结论仍然可信，所以退 0。

---

## issue 驱动：一条评论换一个 PR

在 GitHub 上给一个 issue 评论 `/aifix`，然后：

```
读事件载荷 → 判授权 → 让模型写一条复现测试 → 跑一遍确认它真的红了
    → 提交这条测试 → 跑核心循环 → 推分支 → 开 PR
```

三条交付通路：

| 情形 | 产出 |
|---|---|
| 写不出复现（或复现不成立） | 只回帖说明缺什么。不建分支、不开 PR |
| 写出了复现但没修好 | **照样开 PR**，标题标明「未修复」—— 一条红着的复现测试本身就是产出 |
| 修好了 | 开 PR，报告写进正文 |

这一层的**每一个判定都是零 LLM 的**：谁有权触发、命令怎么解析、走哪条交付通路，
全部由确定性代码决定。模型只负责写复现测试。

授权是刻意收得很紧的 —— 只有**仓库所有者**、在**他自己提的 issue** 下、评论
**第一行**恰好是 `/aifix` 才会触发。第二条不是权限洁癖：issue 正文会作为输入交给
模型，而外人提交的正文是不可信文本。（代价是**组织名下的仓库开箱跑不通**，
见接入教程里那一节。）

- **想接到自己的项目上** → [docs/integration.md](https://github.com/sumengnan/aifix-code/blob/main/docs/integration.md)（一步步的接入教程，
  含可直接复制的 workflow）
- **想弄明白这条流水线内部怎么走** → [docs/issue-driven.md](https://github.com/sumengnan/aifix-code/blob/main/docs/issue-driven.md)

---

## 它到底行不行：评测读数

aifix 自带一套评测：从 git history 里挖出**真实的「红转绿」提交**当任务
（`aifix mine`）。任务自带 ground truth —— 那个提交改过的源文件就是标准答案，
不需要人来标注。

39 个任务（来自两个真实仓库），四轮读数：

| 模型 | 任务数 | 定位准确率 | 修复成功率 | 平均成本 |
|---|---|---|---|---|
| deepseek-v4-flash | 37 | 51% (19/37, CI 36%–67%) | 27% (10/37, CI 15%–43%) | $0.4316 |
| qwen3-coder-flash（旧工具面） | 39 | 90% (35/39, CI 76%–96%) | 18% (7/39, CI 9%–33%) | $0.2345 |
| qwen3-coder-flash（新工具面） | 39 | 92% (36/39, CI 80%–97%) | 74% (29/39, CI 59%–85%) | $0.1311 |
| qwen3-coder-flash（换补丁重算实现） | 39 | 90% (35/39, CI 76%–96%) | **79%** (31/39, CI 64%–89%) | **$0.1241** |

**每个百分比都带置信区间**，这不是排版讲究：只给比率的话，`1/1` 的 100% 和
`12/20` 的 60% 长得一样重，而这个项目已经从噪声里读出过一次假结论并公开更正。

第二行到第三行是一次**干净的对照**：同一个模型、同一批任务、同一份预算，
**只改了工具面**，修复成功率 18% → 74%，两个置信区间完全不重叠。起因是一组
读数：`apply_patch` 被调用 332 次、失败 309 次，而能解析的坏补丁里 **247/247 是
`@@ -a,b +c,d @@` 里的行数与正文对不上**。模型的正文往往是对的，它栽在数数上。

于是加了三个工具、改了一处输入：

- `edit_file` —— 给原文和新文，不写 diff、不数行号
- `read_symbol` —— 按名字读一个函数/类的完整定义，边界由 AST 算出来
- `read_file` 带 `offset` —— 截断消息里给出下一段从第几行开始
- 诊断模型现在能看见候选位置的**真实源码**，而不只是路径和行号

同时 token 降 45%、成本降 44%：省掉的正是「在读文件 / 打补丁的死循环里空转」
那一部分。

完整方法、四轮明细、以及两处**作者自己读错然后更正**的记录，见
[docs/evaluation.md](https://github.com/sumengnan/aifix-code/blob/main/docs/evaluation.md)。

---

## 跑不起来时先看这里

| 症状 | 多半是 |
|---|---|
| 「没有适配器认领这个项目」 | 目录里既没有 `pom.xml`，也没有 `pyproject.toml` / `tests/` |
| 「工作区不干净，请先提交或 stash」 | 已跟踪文件有未提交的改动（未跟踪文件不算） |
| 「模型端点不可达」 | key / base_url 配错，或这台机器出不了网。GitHub runner 上还要确认端点**没有 IP 白名单** |
| 「本次 baseline 的 N 个失败里有 M 个是整个测试文件没能跑起来」 | 测试解释器里没装目标项目的依赖 —— 配 `AIFIX_TEST_PYTHON` |
| 「跑目标项目的测试**超时**了」 | 调大 `AIFIX_TEST_TIMEOUT_SECONDS`（默认 1800 秒） |
| 「拒绝启动：设置了美元预算上限，但没有配置价格表」 | 配 `AIFIX_PRICE_MAP`，或改用 `AIFIX_BUDGET_TOKENS` |
| 报告说修好了，但分支上什么都没有 | 不该发生 —— 这条路上有守卫，请把 `.aifix/runs/<run_id>/` 留下来 |

跑完想知道模型每一步在干什么：

```bash
aifix replay <run_id> --repo /path/to/repo
aifix replay <run_id> --step 7 --full   # 只看第 7 步，不截断
```

诊断工具的完整用法见 [docs/diagnostics.md](https://github.com/sumengnan/aifix-code/blob/main/docs/diagnostics.md)。

---

## 更多文档

| 文档 | 讲什么 |
|---|---|
| [docs/integration.md](https://github.com/sumengnan/aifix-code/blob/main/docs/integration.md) | **接入教程**：怎么把 aifix 接到你自己的项目上 |
| [docs/architecture.md](https://github.com/sumengnan/aifix-code/blob/main/docs/architecture.md) | 状态图、每个节点在做什么、模块地图、为什么这么分 |
| [docs/cli.md](https://github.com/sumengnan/aifix-code/blob/main/docs/cli.md) | 全部命令的参数与退出码语义 |
| [docs/configuration.md](https://github.com/sumengnan/aifix-code/blob/main/docs/configuration.md) | 所有环境变量、默认值、以及每个旋钮的取舍 |
| [docs/safety.md](https://github.com/sumengnan/aifix-code/blob/main/docs/safety.md) | 守卫、四层围栏、三层预算、不可逆动作清单 |
| [docs/adapters.md](https://github.com/sumengnan/aifix-code/blob/main/docs/adapters.md) | 适配器协议、pytest / Maven 两个实现、怎么加第三个 |
| [docs/evaluation.md](https://github.com/sumengnan/aifix-code/blob/main/docs/evaluation.md) | 评测方法、怎么读那张对比表、已知的口径偏差 |
| [docs/issue-driven.md](https://github.com/sumengnan/aifix-code/blob/main/docs/issue-driven.md) | issue → PR 流水线、GitHub Actions 配置、四个 workflow |
| [docs/diagnostics.md](https://github.com/sumengnan/aifix-code/blob/main/docs/diagnostics.md) | trace / replay / ingest / stats，以及事实的数据契约 |
| [docs/superpowers/specs/](https://github.com/sumengnan/aifix-code/tree/main/docs/superpowers/specs/) | 原始设计规格（写代码之前的评审稿） |
| [docs/superpowers/plans/](https://github.com/sumengnan/aifix-code/tree/main/docs/superpowers/plans/) | 六个里程碑的实现计划 |

---

## 项目结构

```
src/aifix/
├── cli.py            命令行入口 + run_once（真正的主循环）
├── graph.py          状态定义、LangGraph 装配、熔断判据
├── config.py         全部配置项（pydantic-settings，读 AIFIX_ 环境变量）
├── verify.py         三态判定 —— 二十行，系统里唯一有资格说「修好了」的地方
├── budget.py         三层预算：全局 → 单 failure → 单次 AgentLoop
├── delivery.py       worktree 隔离与交付提交
├── signals.py        补丁合理性的静态信号（纯 AST，不改判定）
├── nodes/            五个节点：preflight / baseline / detect / fix / verify / report
├── agents/           三个 agent 的提示词与输出解析：detector / fixer / reproducer
├── tools/            8 个工具 + 共用的写入守卫
├── adapters/         项目适配器：pytest / maven / JUnit XML 解析
├── eval/             评测：挖任务 / 变异 / 跑批 / 打分 / 区间估计
├── issue/            issue 驱动：授权判定 / GitHub 客户端 / 流水线编排
├── reproduce.py      缺陷报告 → 复现测试 → 红检
├── trace.py          三层嵌套 trace，事实与事件分开落盘
├── trajectory.py     跨 run 汇总：facts.jsonl → SQLite
├── replay.py         把一次 run 渲染成可读的时间轴
└── progress.py       跑到一半时终端上看得见什么
```

代码里的注释密度很高，而且大多写的是**为什么**，不是「这一行做了什么」——
很多段落记的是一次真实事故和它的读数。读代码时那些注释比这份 README 更精确。

---

## 依赖

- [ai-harness-framework](https://github.com/sumengnan/ai-harness-framework) ≥ 0.0.3
  —— 自研的 agent 框架：模型接入、工具循环、打转检测、预算、事件流、沙箱抽象
- langgraph ≥ 1.2 + langgraph-checkpoint-sqlite
- pydantic ≥ 2.7

分工边界：`harness/` 里不出现 `pytest`、`failure`、`patch` 这些词，它只知道
「模型、工具、循环、预算」；领域知识全在 aifix 这一层。

---

## 参与开发

```bash
git clone https://github.com/sumengnan/aifix-code.git
cd aifix-code
uv sync
uv run pytest -q -n auto      # 925 个用例，并行约 3 分钟
```

`main` 与 PR 上会自动跑 Python 3.11 / 3.12 / 3.13 三个版本
（[tests.yml](https://github.com/sumengnan/aifix-code/actions/workflows/tests.yml)）。
Maven 那几条用例在没有可用 `mvn` 的机器上会自己跳过 —— 判据是「`mvn -o` 跑不跑得
起来」，不是「`mvn` 在不在」。

### 发新版本

发布走 **PyPI Trusted Publishing**（OIDC），仓库里没有、也不需要任何 API 令牌。

```bash
# 1. 改 pyproject.toml 里的 version，提交
# 2. 打 Release —— 剩下的全自动
gh release create v0.1.1 --generate-notes
```

`release.yml` 会依次做：三个 Python 版本跑全量测试 → **核对 tag 与 pyproject 版本
是否一致** → `uv build` → `twine check` → 上传 PyPI。

两处刻意的闸：

- **版本号对不上就停。** 不查的话，标签打成 `v0.2.0` 而 pyproject 还写着 0.1.0 时，
  发出去的是 0.1.0 —— PyPI 报一句「File already exists」，错在标签，消息却指向别处。
- **传之前 `twine check`。** 构建成功不等于传得上去，而那时 Release 已经发出去了，
  看起来像「发布成功了但包没上去」。

> **PyPI 不允许重传同一个版本号。** 发错了只能发下一个补丁号，而错的那个仍然挂在
> 上面能被装到 —— 这就是为什么测试挡在发布前面，而不是发完再补跑。
