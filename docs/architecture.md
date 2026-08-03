# 架构：一次 run 里到底发生了什么

这份文档讲清楚三件事：**状态图长什么样**、**每个节点在做什么**、**为什么这么分**。

读之前先记住一句话，它是整个设计的地基：

> 只有零 LLM 的确定性代码有资格说「修好了」。

---

## 目录

- [两层，互不知道对方](#两层互不知道对方)
- [状态图](#状态图)
- [逐个节点](#逐个节点)
- [三个 agent 的分工](#三个-agent-的分工)
- [状态对象：AifixState](#状态对象aifixstate)
- [中止的四种「种类」](#中止的四种种类)
- [为什么有两条执行路径](#为什么有两条执行路径)
- [模块地图](#模块地图)

---

## 两层，互不知道对方

```
┌─────────────────────────────────────────────────┐
│ aifix-code（这个仓库）                           │
│   状态图 · 三个 agent · 项目适配器 · 三态判定     │
│   worktree 交付 · 评测 · issue 流水线            │
└─────────────────────────────────────────────────┘
                     │ 依赖
                     ▼
┌─────────────────────────────────────────────────┐
│ ai-harness-framework（独立的包）                 │
│   模型接入 · 工具循环 · 打转检测 · 预算           │
│   事件流 · 沙箱抽象 · 快照 · 审批                │
└─────────────────────────────────────────────────┘
```

**分工的判据很干脆**：`harness/` 里不出现 `pytest`、`failure`、`patch` 这些词。
它只知道「模型、工具、循环、预算」，一切与「修 bug」有关的知识都在 aifix 这一层。

**状态也是两层，而且互不知道对方**：

- **宏观状态**（`AifixState`）：跨 failure 的进度 —— 队列里还剩谁、这是第几轮、
  已经花了多少。归 aifix 管。
- **微观状态**：单次 `AgentLoop` 内部的消息历史、工具调用、步数。归框架管。

两层之间只有一个接口：`agents/runner.py` 的 `consume()`，它把框架吐出来的异步
事件流收敛成一个 `AgentOutcome`（文本、token 数、成本、事件列表、有没有被成本闸
掐断）。

---

## 状态图

```
             ┌──────────┐
             │ preflight│  探测适配器 / 工作区干净 / 测试解释器可用      零 LLM
             └────┬─────┘
                  │ 不通过 → report（退出码 1）
                  ▼
             ┌──────────┐
             │ 模型探针 │  往 fixer 那条路由发一次最小调用             极小调用
             └────┬─────┘  （--dry-run 或调用方注入了 client 时跳过）
                  │ 不通 → report（退出码 1）
                  ▼
        ╔═════════════════════╗
        ║ 建 worktree + 分支   ║  .aifix/runs/<run_id>/tree，分支 aifix/<run_id>
        ╚══════════╤══════════╝
                   ▼
             ┌──────────┐
             │ baseline │  跑全量测试 → JUnit XML → 失败集合           零 LLM
             └────┬─────┘
                  │ 全绿 → report（「没活干」）
                  │ 全是收集错误 → report（环境故障，退出码 1）
                  ▼
        ┌─────── 取队列里的下一个 failure ◄──────────┐
        │                                            │
        ▼                                            │
   ┌─────────┐                                       │
   │ detect  │  traceback + 真实源码片段 → 诊断 JSON  │  单步、无工具
   └────┬────┘                                       │
        ▼                                            │
   ┌─────────┐                                       │
   │  fix    │  读码 / 搜索 / 改代码 / 跑目标用例      │  多步、8 个工具
   └────┬────┘  改动为空或过大 → 带反馈重试            │
        │       模型提问了 → 回滚并中止整个 run        │
        ▼                                            │
   ┌─────────┐                                       │
   │ verify  │  再跑全量 → 抖动过滤 → 三态判定         │  零 LLM
   └────┬────┘  BETTER → commit；否则 rollback        │
        │                                            │
        └─ 没修好且 attempt < 3 → 同一个 failure 再来 ─┘
        │  修好了 / 用尽轮次 → 下一个 failure
        ▼
   ┌─────────┐
   │ report  │  渲染 Markdown，落盘 .aifix/runs/<run_id>/report.md
   └─────────┘
```

每一轮循环开始前还有两道横切的闸：

- **预算检查**（`budget.exhaustion()`）：token / 美元 / 墙钟任一越线就中止
- **熔断检查**（`check_circuit_breaker()`）：连续 3 个 failure 一个没修好就中止

---

## 逐个节点

### preflight —— 把能一秒钟确定的前提，挡在几分钟的等待之前

`src/aifix/nodes/preflight.py`

三条检查，任一不满足就中止整个 run：

1. **显式配置的测试解释器可用吗**（`AIFIX_TEST_PYTHON` 指向的文件存在且可执行）
2. **有适配器认领这个仓库吗**（按注册表顺序探测：Maven → vitest → pytest）
3. **主工作区干净吗**（已跟踪文件不得有未提交改动）

**为什么第一条要在这里拦**：到了 baseline 那一步，解释器不可用的表现是「没写出
JUnit 报告」，用户看到的消息是「测试进程没能正常跑完」—— 一句指向目标项目的话，
而真相是 aifix 的配置写错了。这个项目里最贵的失败一向不是崩溃，是**指错方向的
诊断**。

**为什么只看已跟踪文件**：worktree 是从 HEAD 创建的，主工作区的未跟踪文件
（`__pycache__`、`.venv`、编辑器临时文件）根本进不去 agent 的工作区。为它们中止，
会让任何跑过一次测试的项目直接用不了这个工具。

紧接着是**模型探针**（`probe_model`）：往 fixer 那条路由发一次 `"ping"`，只读第一
个 chunk。不做这一步的话，端点不通的表现是每一轮都修不好 → 重试到上限 → 熔断 →
报告写「疑似系统性问题」。跑了几十分钟，最后给一句指错方向的诊断。

`--dry-run` 时跳过探针：那个开关的承诺是「不花一分钱、不调用任何模型」，探针也是
模型调用。

### baseline —— 整个 run 唯一一次「改动之前长什么样」的测量

`src/aifix/nodes/baseline.py`

在 worktree 里跑一次全量测试，解析 JUnit XML，得到失败集合。**全量测试很贵，
所以只在这里跑一次**，之后每轮 verify 各跑一次。

> **这几遍全量是一次 run 里最大的时间开销。** 实测（2026-08-03，本仓库 956 个用例）：
> 串行一遍 7 分 12 秒，三遍就是 21 分钟。所以全量默认走 pytest-xdist 并行
> （`AIFIX_TEST_PARALLEL=auto`，探不到 xdist 就静默串行），实测降到 3 分 58 秒。
> 复跑那一跑**不并行** —— 一两个用例起 N 个 worker 是纯开销。见
> [configuration.md](configuration.md#aifix_test_parallel)。

这个节点有三道相当细的闸：

**1. 报告必须存在，而且里面真的跑了用例**（`require_report=True`）

「没跑成」不能冒充「跑完了、全绿」，而它有两种形状：

- 一份报告都没有 → 进程没跑完（超时被杀、崩溃、命令没执行起来）
- 报告在，里面零个用例 → 进程正常退出了，是**收集**没成功（node id 无效、
  conftest 抛异常、依赖缺失）。pytest 此时退 4 并写出一份 `tests="0"` 的报告

两种形状的排查方向完全不同，所以消息分开写。超时还会单独说，并指向具体的旋钮。

**2. 收集错误占比过高 → 判为环境故障并中止**

pytest 在收集阶段整轮中断时**照样写出一份完整的 JUnit 报告**，里面是一条条文件级
`<error>`。它们会被老老实实翻译成可重跑的 node id，进队列 —— 于是模型被派去修
「这台机器上没装某个包」这件事：真花钱，熔断在第 3 个之后中止，而报告写的是
「模型没修好」。一个成绩，其实是一次故障。

判据是两条阈值**同时**成立：文件级 error ≥ 2 条，且严格超过总失败数的一半。

**为什么是中止而不是过滤掉那几条**：收集错误意味着这份 baseline 是**残缺的** ——
pytest 压根没跑到后面的测试，我们不知道仓库其余部分是红是绿。悄悄扔掉几条 id 会把
「测量没做成」伪装成「测量做完了，只是少几条」。

逃生口：`AIFIX_ALLOW_COLLECTION_ERRORS=1`（真的确认那就是仓库自己的 bug 时用），
打开时会往 stderr 出声并往 trace 记一条事实 —— 一条被静默绕过的守卫等于没有守卫。

**3. 补丁可能不可见的警告**

跑 baseline 之前问一句：目标包会不会从 worktree **之外**导入？如果目标项目把自己
可编辑安装进了那个解释器，`import <目标包>` 可能解析到源仓库那份没打补丁的代码。
不崩溃、不报错，只有「修好了」是假的。

只出声、不中止 —— 这道探测是近似的（复现不了 `conftest.py` 里手写的 `sys.path`
改动），拿一个可能误报的信号去拦住整个 run，会让用户为了跑起来而去关掉它。

### detect —— 单步、无工具、强制 JSON

`src/aifix/nodes/detect.py` + `src/aifix/agents/detector.py`

输入：失败用例的断言信息、完整 traceback、适配器确定性推出来的**嫌疑源码位置**
候选，以及前三个候选周围的**真实源码片段**（由 `snippet.around` 读出来，零 LLM，
不多花一个回合）。

输出：一个 JSON 对象 —— 嫌疑文件、嫌疑行号、根本原因、修复思路、置信度。

**为什么给它真实源码**：在 `snippet.py` 出现之前，诊断模型判断「根本原因是什么」
时看到的只有路径、行号和 traceback 的措辞 —— 那段代码它从未见过，`suspect_lines`
更是纯粹编的。而编出来的行号会原样进入修复模型的开场白（「嫌疑行号：120-135」），
把它的第一步引向一个具体而错误的位置。

代码就在磁盘上，读它不需要模型、不需要一个回合、不花一分钱。

**解析失败不是错误，是降级信号**：`parse_diagnosis` 返回 `None` 时，调用方改为把
原始 traceback 直接交给修复模型自行判断。

**候选的来源要标出来**（`SourceCandidate.origin`）：

- `traceback` —— 失败真的穿过这一帧。最强的证据。
- `import` —— 测试文件 import 了它。弱一档：「测试用到了这个模块」不等于「缺陷在
  这个模块」。纯断言失败时栈上根本没有源码帧（被调函数正常返回了），这是唯一还
  拿得到的确定性锚点。

一个源码候选都没有时，`suspect_anchored=False`，下游的「改动落在嫌疑文件之外」这条
信号会关掉 —— 没有参照系就不比。

### fix —— 多步、8 个工具、两道后置守卫

`src/aifix/nodes/fix.py` + `src/aifix/agents/fixer.py`

工具面（白名单，没有 shell、没有网络）：

| 工具 | 干什么 |
|---|---|
| `read_symbol` | 按名字读一个函数/类的完整定义，边界由 AST（或括号计数）算出来 |
| `read_file` | 读文件，带行号，支持 `offset`/`limit` 分段 |
| `list_files` | 列目录 |
| `grep` | 按正则搜（底层 `git grep`，自动跳过 .gitignore） |
| `edit_file` | 给原文和新文做替换 —— **改代码的首选** |
| `apply_patch` | 应用 unified diff —— 只在要一次动多个文件时用 |
| `run_tests` | 跑目标失败用例（只能跑失败列表里的，一次最多 5 个，不能跑全量） |
| `ask_user` | 无法判断「什么才算正确行为」时停下来问人（可选，见下） |

**为什么 `edit_file` 是首选**：unified diff 要求模型逐字复述上下文行、数对两侧行数、
给对起始行号 —— 把「我要把这段改成那段」编码成了一道算术题，而算术正是 LLM 结构性
最弱的能力。实测一轮评测：`apply_patch` 调用 332 次失败 309 次，能解析的坏补丁里
247/247 是 `@@` 里的行数与正文对不上。

`edit_file` 把记账拿走：没有行号、没有计数，能算错的东西不存在。代价是必须原样复述
要改的那一段 —— 那是复制，不是计算。

**为什么 `run_tests` 只能跑目标用例**：给全量的话，模型会对整体健康度下判断，
而那是 verify 的职权。

**`ask_user` 只在有人能回答时才注册**：`aifix eval` 并行跑几十个任务、没有任何人在
看，注册它等于给模型一条烧钱的岔路 —— 把一整轮花在一个永远等不到回复的问题上，然后
被判成没修好。带着答复重跑的那一轮也不给（答案就在开场白里）。

**两道后置守卫**，都以「带反馈重试」的方式处理，而不是直接失败：

| 守卫 | 触发条件 | 反馈 |
|---|---|---|
| `empty_diff` | 改动行数为 0 | 「你没有对任何文件做出修改，只说『已修复』是无效的」 |
| `huge_diff` | 改动行数 > 300 | 「改动范围过大，疑似整文件重写，已回滚，请只改必要的几行」 |

守卫重试**不计入 attempt** —— attempt 衡量的是「修复尝试」，而这里连一次有效尝试
都还没产生。同一条守卫连续触发 2 次就放弃这个 failure（把「钱花完了」变成「出问题
了，去看 trace」，省下的额度还能流给真有希望的 failure）。

`empty_diff` 有一个说实话的分支：如果模型确实改了文件，只是改到了被 `.gitignore`
盖住的路径上，反馈会明说这件事。说它「没有做出修改」是一句假话，而模型照那句话去做
只会再改一次同样的东西，一路重试到放弃。

### verify —— 系统里唯一有资格说「修好了」的地方

`src/aifix/nodes/verify.py` + `src/aifix/verify.py`

**零 LLM。** 判定逻辑是这样：

```python
def compare(baseline, current, target):
    new = current.ids - baseline.ids
    if new:                                    # 任何新的红 → 一律 WORSE
        return Verdict.WORSE
    if target in (baseline.ids - current.ids): # 目标从红变绿
        return Verdict.BETTER
    return Verdict.SAME
```

`new` 的判断在最前：**即使目标用例修好了，只要引入任何新失败一律 WORSE**。
比「净改善」保守得多，但这正是敢在真实仓库上跑的前提。

判定之前有一步**抖动过滤**：出现新失败时，只重跑那几个用例确认一次。重跑还红的算
确认回归；重跑绿了的算抖动，从当前结果里剔除。成本近似为零，却挡掉了绝大部分
「把一个本来正确的补丁滚掉」—— 那是这个系统最昂贵的错误。

还有几道细闸：

- **一个字节都没改却判 BETTER** → 降级为 SAME。那说明目标用例在 baseline 里本来就
  是抖的，放任不管的话系统会宣称修好了一个它没碰过的 bug。
- **交付失败要接住，不能裸抛**。`git add -- <路径>` 有两种真实的失败形态（路径匹配
  不到 → 退 128 且一条都不暂存；新文件命中 .gitignore → 退 1 而别的路径已经暂存）。
  裸抛的后果不是「报错」而是**失联**：worktree 被删、报告根本执行不到，用户拿到一段
  调用栈，而本次 run 前面几个 failure 已经提交进交付分支的修复也没人告诉他。
- **判定要用 commit 的返回值**：「分支上到底有没有多一个提交」只有 git 有资格回答。
  提前用 `git diff` 猜会漏掉新增文件（diff 看不见未跟踪文件），而新建一个源文件是
  完全合法的修复。

### report —— 报告是用户手里唯一的成果凭据

`src/aifix/nodes/report.py`

worktree 退出时就被删了。分支上有没有东西、修好了几个、下一步该干什么，全写在报告
里。所以 `run_once` 的 `except` 分支不吞异常，但**保证报告先落地**：记成一次中止、
渲染、落盘，然后照常返回，退出码由 CLI 负责。

报告里有几处刻意的处理：

- **成本算出 0 却花了 token** → 写「未知（未配置 AIFIX_PRICE_MAP）」，不写 `$0.00`
- **收集错误中止时不写「修复 0 / 11」** → 那一行长得和一个成绩一模一样，而分母是
  一批本就不该存在的工单数。改成「修复 —（baseline 不可信，一个用例都没开修）」
- **一个都没修好时不给 `git merge` 命令** → 那条分支与 HEAD 逐字相同，给命令是在
  邀请用户去合一个空分支
- **待答的问题排在最前面** → 这次 run 的产出就是这个问题，塞在表格底下等于让人自己去找
- **「值得多看一眼」一节只在真有信号时出现**，且按 test_id 分组 —— 恒定出现的一节
  会被当成模板噪音无视掉

---

## 三个 agent 的分工

| | detector | fixer | reproducer |
|---|---|---|---|
| 步数 | 1 | 25 | 25 |
| 工具 | 无 | 8 个（含写入） | 4 个（**只读**） |
| 输出 | 强制 JSON | 补丁（经由工具落盘） | 强制 JSON |
| 什么时候跑 | 每轮 attempt | 每轮 attempt | issue / `reproduce` 命令，在 run 之前 |
| token 上限 | 20,000 | 分到的 failure 额度 | 250,000（独立） |

### 为什么拆成两次调用

合并成一次的话，就没有可独立评测的中间产物了。评测表里的「定位准确率」量的正是
detector 的能力，它与「修复成功率」分开看才有意义 —— 实测里出现过定位 57%、修复 17%
的组合，那说明卡住的不是「找不到文件」，是「改不对」。

### reproducer 为什么是只读的

它的任务是把一段自然语言的缺陷报告翻译成**一条红着的测试**。

- **没有写入工具**：复现测试由确定性代码写下去（`write_reproduction`），不经过工具面。
  给它任何一条写入路径，它就能直接改产品代码去迎合自己写的测试。
- **没有 `run_tests`**：让它自己跑测试的话，「这条测试红不红」的判定权就落到模型手里。
  红检（`red_check`）是这一步唯一的确定性证据，不能交出去。

它的步数**与 fixer 齐平，不是更小**。最初设成 12 的理由是「只有读工具，读够了就该
作答」，实测两个模型都在 12 步用尽而不作答 —— 而回放显示它们没迷路，是在认真准备。
前提错在哪：fixer 拿到的是 traceback 加一份指名道姓的诊断，只需确认那一处；
reproducer 拿到的是一段人话，要把整套测试脚手架逆推出来。**写复现比修 bug 需要更多
探索，不是更少。**

红检把「不算复现」拆成四种，因为下一步动作完全不同：

1. **收集错误** —— import 不到东西也是红，但它复现的是模型自己的笔误
2. **用例没跑出结果** —— node id 对不上，或者被跳过了
3. **跑了但没失败** —— 这条测试在当前代码上就是绿的，约束力为零
4. **红在自己的笔误上** —— 第 1 条的运行时版本。模块 import 得好好的，名字错在测试
   函数体里（典型：用了 `pytest.raises` 却没 `import pytest`），前三道闸逐条放行

第 4 条的判据是**异常抛在哪，不是异常是什么类型**。产品代码里真的引用了未定义的名字，
那是货真价实的缺陷，`NameError` 正是它该有的样子 —— 按类型一刀切会把它一并打回。区别
在栈帧：笔误的栈只到复现测试文件里（那段代码整个是模型写的），真缺陷的栈会穿进产品文件。

最要紧的边界是**真断言失败的栈同样只到测试文件**（被调函数正常返回了，栈上没有它），
而那是最常见的合法复现 —— 所以类型判定同样不能省，两半缺一不可。拿不到栈帧时一律放行：
把「没有证据」当成「有罪的证据」，会让这道闸在自己瞎掉的时候变得最严厉。

---

## 状态对象：AifixState

`src/aifix/graph.py`。几个值得单独说的字段：

| 字段 | 含义 |
|---|---|
| `baseline_ids` / `queue` / `current` / `attempt` | 跨 failure 的进度 |
| `verdict` | 本轮判定：`better` / `same` / `worse` |
| `ask` | 模型停下来问的那个问题（`{test_id, question, options}`） |
| `answer` | 人对上一轮提问的答复，已拼成给模型看的一段话 |
| `touched` | **所有写入工具**记账过的路径 —— 交付时 `git add` 的全部输入 |
| `signals` | 补丁合理性的静态信号列表（**不参与任何判定**，只展示给人看） |
| `failure_token_budget` / `failure_usd_budget` | 本轮 failure 分到的额度 |
| `abort` / `abort_kind` | 中止的**消息**（给人看）与**种类**（给程序判） |
| `_failures` / `_trace` / `_progress` | 下划线前缀 = 不参与路由，只是数据源或侧信道 |

`failure_usd_budget` 有一处必须用 `is None` 判定的地方：`None` 表示「不设美元闸」，
`0.0` 表示「额度已经扣光，一次调用都不许发起」。写成 `x or None` 的话，`0.0 or None`
求值成 `None` —— 恰好把闸最该拦住的那一刻变成完全不拦。

`signals` 是**列表**不是单个 dict：核心循环对每个 failure 各跑一轮 verify，而报告在
整个 run 结束后才渲染，单个 dict 会被后一轮整个替换掉。

---

## 中止的四种「种类」

`abort` 是给人看的消息，`abort_kind` 是给程序判的分类。读它的有三方：报告、CLI 退出
码、评测的成绩/故障分类。

| kind | 什么意思 | 退出码 | 评测里算什么 |
|---|---|---|---|
| `preflight` | 路径不是 git 仓库 / 没有适配器 / 工作区不干净 / 解释器不可用 | 1 | 评测故障 |
| `collect` | baseline 里文件级收集错误占比过高 | 1 | 评测故障 |
| `model` | 配置的模型端点连不上 | 1 | 评测故障 |
| `crash` | 运行时异常 | 1 | 评测故障 |
| `tokens` / `usd` | 预算耗尽 | **0** | 模型的真实成绩 |
| `wall` | 墙钟耗尽 | **0** | 评测故障（那是调度器的属性） |
| `needs_input` | 停在「等人回答」上 | 0 | — |

**为什么预算耗尽退 0**：那是正常收场 —— 活干到钱花完为止，结论仍然可信。

**为什么墙钟单独算一类**：token 和美元预算是**模型**的属性（同一批任务、同一个上限，
谁先烧完谁差，可比）；墙钟是**评测调度器**的属性（`--parallel 8` 时八个任务抢同一台
机器的 CPU，墙钟耗尽的概率远高于 `--parallel 1`）。把它记成模型的失败，等于「只改
并行度就能改变修复成功率」，直接违背跨模型对比的前提。

**这份清单漏一项的后果是静默的**：`_cmd_run` 的退出码按 kind 判，不在集合里的就退 0
—— preflight 这一项曾经漏了一整轮，表现是 `aifix run /打错的/路径` 印一句「中止」然后
退 0，流水线里 `aifix run || 报警` 一声不吭，CI 把它读成成功。

CLI 与 issue 两条入口各存一份这个集合（`cli._FAILED_RUN_KINDS` 与
`issue.handle._ENV_ABORTS`），是有意的：两条入口的判据**可以**分叉。代价是要靠
`tests/test_abort_kind_parity.py` 把它们钉在一起。

---

## 为什么有两条执行路径

`graph.py` 里有一个 `build_graph()`，装配出一张真正的 LangGraph；而产品入口走的是
`cli.run_once()` —— 一个手工驱动节点的 `while` 循环。

**两条路径不等价，这一点必须说清楚**：`RunBudget`、单 failure 的额度分配、「越线即
中止」的检查，全部只写在 `run_once` 里。`build_graph()` 那条路径没有 `RunBudget`，
`failure_usd_budget` 一直是 `None`，整条美元闸不存在。

所以：**图那条路径目前只用于结构验证，别拿它去验证任何与花钱有关的保证。**

节点顺序与路由和图完全一致，把 LangGraph 的 checkpointer 真正接进主循环是后续的事
（`AIFIX_ENABLE_CHECKPOINT=1` 会在产物目录下留一个 sqlite 文件）。

---

## 模块地图

```
src/aifix/
│
├─ 主循环
│  ├─ cli.py            命令行入口 + run_once（真正的主循环，预算在这里）
│  ├─ graph.py          AifixState、abort_kind 常量、熔断判据、LangGraph 装配
│  └─ nodes/            preflight · baseline · detect · fix · verify · report
│
├─ 判定与交付
│  ├─ verify.py         三态判定 —— 二十行，零 LLM
│  ├─ delivery.py       worktree 隔离、精确提交、显式署名
│  └─ signals.py        补丁合理性的静态信号（纯 AST，不改判定）
│
├─ 模型侧
│  ├─ agents/detector.py    提示词 + Diagnosis 模型 + 容错解析
│  ├─ agents/fixer.py       提示词 + 工具注册 + 开场白组装 + 定位提示
│  ├─ agents/reproducer.py  提示词 + Reproduction 模型 + 自洽性校验
│  └─ agents/runner.py      事件流 → AgentOutcome（两层之间唯一的接口）
│
├─ 工具面
│  ├─ tools/guard.py    写入前的三道检查，所有写入工具共用这一份
│  ├─ tools/read.py     带行号、带 offset、截断消息给出下一段
│  ├─ tools/read_symbol.py  AST / 括号计数算出符号边界
│  ├─ tools/edit.py     原文替换，报错时把文件真实内容还回去
│  ├─ tools/patch.py    unified diff，--recount 从正文重算行数
│  ├─ tools/search.py   git grep
│  ├─ tools/tests.py    只能跑失败列表里的用例
│  └─ tools/ask.py      停下来问人，三道硬约束
│
├─ 项目适配
│  ├─ adapters/base.py       协议：四个问题 + 一个真活
│  ├─ adapters/junit.py      JUnit XML → FailureSet（公分母）
│  ├─ adapters/pytest_adapter.py
│  ├─ adapters/maven_adapter.py
│  └─ adapters/vitest_adapter.py
│
├─ 可观测
│  ├─ trace.py         三层嵌套 span，事实与事件分开落盘
│  ├─ traces.py        把结论推到孤儿分支，让它活过 CI runner
│  ├─ trajectory.py    facts.jsonl → SQLite，跨 run 查询
│  ├─ replay.py        渲染成可读的时间轴
│  ├─ progress.py      跑到一半时终端上看得见什么
│  └─ violations.py    从事件流里数「越界尝试」
│
├─ 评测
│  ├─ eval/task.py       Task / TaskResult 数据模型 + jsonl 读写
│  ├─ eval/mine.py       从 git history 挖真实的红转绿 commit
│  ├─ eval/mutate.py     人造变异（冒烟集，不是基准）
│  ├─ eval/workspace.py  把一个任务还原成可直接跑的仓库
│  ├─ eval/runner.py     单任务执行 + 并行调度 + 整批预算
│  ├─ eval/score.py      双档打分与对比表
│  └─ eval/stats.py      Wilson 区间
│
└─ issue 驱动
   ├─ issue/event.py    授权判定（零 LLM）
   ├─ issue/github.py   gh CLI 薄壳
   ├─ issue/handle.py   流水线编排
   ├─ reproduce.py      缺陷报告 → 复现测试 → 红检
   └─ pending.py        待答问题的两种持久化（文件 / 评论标记）
```

### 几处「只能有一份」的实现

这个项目吃过好几次「同一个判定有两份实现，然后各自漂移」的亏。现在被强制收敛的有：

| 判定 | 唯一实现 | 分家的后果 |
|---|---|---|
| 适配器探测 | `nodes/baseline.detect_adapter` | `aifix mine` 曾写死 `PytestAdapter()`，对 Maven 工程产出 0 个任务且不报错 |
| 「这个文件在不在测试目录里」 | `signals.under_dirs` | 守卫停在 `parts[0] in test_dirs`，而 Maven 的 test_dirs 是 `["src/test"]` —— 最核心的守卫静默失效 |
| 「两条路径是不是同一个文件」 | `signals.same_file` | 同一对路径可能在定位准确率里算命中、在越界信号里算越界 |
| 「这个 id 是不是文件级」 | `adapter.is_file_level_id` | `eval/mine` 曾写死 `"::" not in i`，于是每一个 Maven id 都被判成文件级 |
| 写入前的三道检查 | `tools/guard.guard_write` | 多一条写入路径而守卫各写各的，迟早漏掉一项 |
| 「修好了几个」 | `nodes/report.count_fixed` | 报告里的数与落进 trajectory 的 `fixed` 列会分家，而分家之后两个数都还是「看着对」 |

---

## 延伸阅读

- [safety.md](safety.md) —— 守卫、四层围栏、三层预算、不可逆动作清单
- [adapters.md](adapters.md) —— 适配器协议与三个实现
- [diagnostics.md](diagnostics.md) —— trace / replay / stats
- [superpowers/specs/2026-07-27-aifix-code-design.md](superpowers/specs/2026-07-27-aifix-code-design.md)
  —— 原始设计规格，含被否决方案的记录
