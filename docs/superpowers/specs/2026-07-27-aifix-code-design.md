# aifix-code 设计规格

日期：2026-07-27
状态：已评审，待实现

---

## 1. 目标与非目标

### 做什么

一个自我改进的 agentic loop：**红色的测试进去，验证过的补丁出来**。系统自己定位问题、自己改、自己验证，人只在最后决定要不要合并。

### 优先级

| 序 | 目标 | 含义 |
|---|---|---|
| 1 | 自己真要用的工具 | 敢在真实 repo 上跑；稳定性 > 演示效果 |
| 2 | 求职作品 | demo 可跑、README 讲得清、有一个亮点做深 |
| 3 | 学习练手 | 亲手写 harness 工程的核心部分 |

优先级 1 决定了所有取舍：**判定权不在模型手里、绝不碰主工作区、能力面白名单**。

### 明确不做

- 不做 Web UI（终端 + JSONL + 可选 OTLP 导出）
- 不做 Reviewer agent（审查者是人）
- 不做主动扫描驱动（lint / 覆盖率缺口自动排队修）
- 不 push、不改主工作区、不删分支（完整清单见 §10）

---

## 2. 关键决策记录

记录被否决的方案，避免实现期反复。

| 决策 | 选择 | 否决项与理由 |
|---|---|---|
| 问题来源 | 测试失败驱动 | 任务/issue 驱动（第二阶段）；主动扫描（砍掉，是 cron 触发器不是独立设计） |
| 交付形态 | worktree 隔离分支 + 报告，人决定 merge | 自动开 PR（等价，加网络依赖，随时可加）；直接改工作区并 commit（要评测数据证明可靠后才配拥有） |
| 项目适配 | 通用适配层，pytest + Maven 双实现 | 只做 pytest（单实现的抽象不是抽象） |
| 编排 | LangGraph | 自己写循环（可行但状态机已定型，checkpoint/时间旅行白拿）；DeepAgents / Claude Agent SDK（它们就是要造的东西）；LangChain（模型层与自有适配器重叠） |
| 模型接入 | 自有 `ai-harness-framework` 的 `OpenAICompatibleClient` | Anthropic SDK（国内不便 + 想自写通信层 + 要 provider 无关）；LiteLLM（避开一个 SDK 却引入更大抽象层） |
| Agent 分工 | Detector（无工具）+ Fixer（有工具），Verifier 为纯代码 | 合并为单次调用（失去可独立评测的中间产物） |
| Fixer 能否跑测试 | 能，但只能跑目标用例 | 不给（盲改）；给全量（模型会对整体健康度下判断，那是 verify 的职权） |
| Shell 工具 | 不给 | 给 `run_shell` + 黑名单（黑名单永远挡不全；任务窄、风险高，应白名单） |
| 审批点 | 只有最终 merge | 中途设审批（worktree 让中途错误零代价；审批点多了人会盲目点确认） |

---

## 3. 架构总览

项目分两层，上层已是独立的包：

```
ai-harness-framework（已发布，github.com/sumengnan/ai-harness-framework）
  模型接入 · 工具循环 · 打转检测 · 预算 · 快照 · 审批 · 事件流 · 沙箱抽象
        ▲ 依赖（只装核心 4 个依赖，不要 [all]）
        │
aifix-code
  LangGraph 状态图 · Detector/Fixer · 项目适配器 · 三态判定 · worktree 交付 · 评测
```

框架已覆盖十三条失败模式中的七条（§10）。本项目负责其余六条，全部是领域特定的。

**分工原则**：`harness/` 不出现 `pytest`、`failure`、`patch` 这些词；它只知道"模型、工具、循环、预算"。这条边界在框架侧由 `tests/test_architecture_layering.py` 用 AST 守住。

---

## 4. 状态图与数据流

```
preflight    探测适配器 / git 干净检查 / 建 worktree + 分支          无 LLM
    ↓
baseline     跑全量测试 → JUnit XML → FailureSet(baseline)          无 LLM
    ↓            全绿 → END（"没活干"）
┌─→ detect   失败用例 + locate_source 候选 → 结构化诊断              LLM，无工具
│      ↓
│   fix      读码 / 搜索 / 打补丁 / 跑目标用例                        LLM + 工具
│      ↓
│   verify   跑全量 → 与 baseline 三态比对                           无 LLM ★
│      ↓
│   BETTER → commit 本轮 ─┐
│   SAME/WORSE → rollback ─┤
│   attempt 超限 → give_up ┤
└──── 队列非空 ────────────┘
    ↓ 队列空
report       diff + 测试前后对比 + trace 摘要 + 成本                 无 LLM
```

### 两层状态，互不知道对方

- **LangGraph 状态**（跨 failure 的宏观）：队列、当前 failure、attempt 计数、baseline、全局预算余额。配 `SqliteSaver`，崩了能从任意节点续跑。
- **`AgentLoop` 状态**（单次 agent 调用内的微观）：消息历史、步数、工具调用。框架自带 checkpoint。

`detect` / `fix` 节点各消费一次 `AgentLoop.run()` 的事件流，转成 LangGraph 状态更新后返回。

### 三条不可动摇的原则

1. **`verify` 零 LLM**：跑测试、解析 XML、比对集合，全是确定性代码。系统里唯一有资格说"修好了"的组件是最笨的那个。
2. **三态判定**：BETTER / SAME / WORSE，不是通过/失败。真实情况常是"修好目标、弄坏另外两个"，二值判定会误判成成功。
3. **失败即回滚**：判定非 BETTER 立即 `git checkout` 回滚本轮改动。否则第 5 轮时 agent 面对的是自己前 4 轮的垃圾。

---

## 5. Agent 分工

### Detector — 无工具、单步、强制 JSON

```python
registry = ToolRegistry()                       # 空注册表：模型必然一步出文本
loop = AgentLoop(client=detector_client, registry=registry,
                 context=ContextManager(DETECT_SYSTEM_PROMPT),
                 max_steps=1, budget=BudgetTracker(max_tokens=20_000))
with json_output():                             # harness.llm.openai_compat
    outcome = await consume(loop.run(detect_prompt(failure, candidates)))
diagnosis = Diagnosis.model_validate_json(outcome.text)
```

```python
class Diagnosis(BaseModel):
    suspect_file: str
    suspect_lines: tuple[int, int] | None
    root_cause: str
    fix_strategy: str
    confidence: Literal["high", "medium", "low"]
```

用便宜模型。`json_output()` 走 `response_format={"type": "json_object"}`，要求顶层是对象——`Diagnosis` 满足，不用套信封。

**解析失败降级**：不视为致命，改为把原始 traceback 直接交给 Fixer，损失定位质量但流程不断。

### Fixer — 有工具、多步、强模型

```python
loop = AgentLoop(client=fixer_client, registry=fix_registry,
                 context=ContextManager(FIX_SYSTEM_PROMPT),
                 max_steps=25,
                 budget=BudgetTracker(max_tokens=remaining, max_wall_seconds=600),
                 loop_detect_window=3,
                 tool_result_max_chars=8000,
                 model_name=fixer_config.model)
```

### 为什么拆两次调用

多花一次调用，换一个**可独立评测的中间产物**。"定位对不对"和"修得对不对"是正交能力：合并后只知道没修好，拆开后知道该调 Detector 还是 Fixer——两条完全不同的改进路径（见 §9）。

### 事件消费胶水（`agents/runner.py`）

```python
async def consume(stream) -> AgentOutcome:
    parts, tokens, cost, error = [], 0, 0.0, None
    async for ev in stream:
        if isinstance(ev, TextDelta):      parts.append(ev.text)
        elif isinstance(ev, ModelUsage):   tokens += ev.usage.total_tokens; cost += ev.cost_usd or 0
        elif isinstance(ev, ToolFinished): trace.record(ev)
        elif isinstance(ev, RunError):     error = ev.error
    return AgentOutcome("".join(parts), tokens, cost, error)
```

### 空 diff 与巨型 diff 兜底（`fix` 节点后置检查）

```python
changed = await sandbox.exec(["git", "diff", "--stat"], timeout=30)
if not changed.stdout.strip():
    return retry_with_feedback("你没有对任何文件做出修改。"
                               "请先用 read_file 确认现状，再用 apply_patch 提交具体改动。")
if diff_line_count(changed.stdout) > config.max_diff_lines:
    return retry_with_feedback("改动范围过大，疑似整文件重写。请只改必要的行。")
```

空 diff 是本领域最常见也最隐蔽的失败：模型自信宣布完成却一字未改。巨型 diff 是模型放弃理解、直接重写整个文件——那种补丁即使测试转绿也不该合。

---

## 6. 工具层与边界

### 能力面是枚举出来的，不是排除出来的

**不给 `run_shell`**。框架有现成的（带危险命令分类 + 人工审批），但本项目不注册。理由：修 bug 所需能力已被下列 5 个工具完整覆盖；给 shell 等于给无限权限面，然后指望黑名单去挡——而黑名单永远挡不全（挡住 `rm -rf`，挡不住 `python -c "shutil.rmtree(...)"`）。

通用做法是"先给 bash 求广度，需管控时再提升为专用工具"。这里反向走：**任务窄、风险高（真实 repo），所以从一开始就白名单**。

`shell/policy.py` 与 `approval.py` 留给第二阶段（任务驱动可能需要装依赖），不是废弃。

### 四层围栏

| 层 | 机制 | 挡住什么 |
|---|---|---|
| 能力 | 只注册 5 个工具，无 shell / 网络 / 包管理 | 枚举之外的一切 |
| 路径 | `resolve_in_workspace()` realpath 归一 | `../`、绝对路径、符号链接逃逸 |
| 进程 | 所有 `exec` 的 cwd 固定为 worktree | 越出目录的相对操作 |
| git | 独立 worktree + 独立分支 | 主工作区完全不可见 |

外加显式禁令：**任何工具不得写 `.git/`**（否则可改 HEAD、删分支、绕过回滚机制）。

### 工具清单

| 工具 | 来源 | 要点 |
|---|---|---|
| `read_file` / `list_files` | 框架 `fs_tools`，零改动 | 需支持 `offset`/`limit` 分段读，防单次读爆上下文 |
| `grep` | 新写 | 底层 `git grep -n`：worktree 内天然可用，自动尊重 `.gitignore` |
| `apply_patch` | 新写 | 只接 unified diff |
| `run_tests` | 新写 | 范围强制，只能跑目标用例 |

#### `apply_patch`

```python
class Params(BaseModel):
    diff: str = Field(description="标准 unified diff，含 --- / +++ 文件头")
```

流程：解析 diff 头取出所有目标路径 → 逐个过 `resolve_in_workspace()` → 拒绝 `.git/` → **拒绝测试目录**（见下）→ `git apply --check` 干跑 → 应用。

失败时把 git 原话（`error: patch failed: foo.py:12`）作为 `ToolError` 喂回。**打不上本身就是廉价的正确性信号**：说明模型对文件现状的理解是错的，这个错误在打补丁阶段暴露，不用等测试跑完。

**拒绝修改测试文件**（默认开启，`--allow-test-edits` 逃生）：否则最省事的"修复"就是删掉断言。测试转绿、判定 BETTER、系统欢天喜地报告修好了——而你会 merge 一个把测试阉掉的补丁。测试目录由 `ProjectAdapter` 提供。

#### `run_tests`

```python
class Params(BaseModel):
    test_ids: list[str] = Field(min_length=1, max_length=5,
                                description="测试标识，须来自当前失败列表")
```

**范围强制是核心**：必填、非空、上限 5、每个 id 必须在 baseline 已知集合内，未知直接 `ToolError` 拒绝。硬超时防测试挂死。

不允许跑全量不只是省 token：全量结果进上下文后，模型会开始对"整个套件的健康度"下判断，而那是 verify 节点的职权。**权威判定必须在 agent 之外。**

---

## 7. 项目适配器

### JUnit XML 是公分母，但只解决一半

| | pytest | Maven Surefire |
|---|---|---|
| 报告文件 | 单个 `report.xml` | `target/surefire-reports/TEST-*.xml`，一类一个 |
| `classname` | 点分模块路径 `tests.test_foo` | 全限定类名 `com.foo.BarTest` |
| `file` / `line` 属性 | 有（xunit2 family） | 无 |
| 重跑单用例 | `pytest -q tests/test_foo.py::test_bar` | `mvn test -Dtest=BarTest#testBar` |

第 3 行关键：**junit xml 的 `classname` 不能直接拿去重跑**。pytest 要文件路径形式，报告给的是点分模块名。这个转换只有适配器知道。

### 接口

```python
class ProjectAdapter(Protocol):
    name: str

    @staticmethod
    def detect(repo: Path) -> bool: ...                    # 配置
    def full_test_command(self) -> list[str]: ...          # 配置
    def scoped_test_command(self, test_ids: list[str]) -> list[str]: ...   # 配置
    def report_glob(self) -> str: ...                      # 配置
    def test_dirs(self) -> list[str]: ...                  # 配置（apply_patch 拒绝这些路径）
    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...
    def locate_source(self, failure: Failure) -> list[SourceCandidate]: ...   # ★ 真活
```

前五条是配置，`make_test_id` 是格式转换，**只有 `locate_source` 是真正的实现工作**。

`junit.py` 共享：JUnit XML → `FailureSet`，两个适配器都用。

### `locate_source`：形状相同，正则不同

```
解析 trace → 过滤出 repo 内部帧（丢弃 site-packages / JDK / 第三方 jar）
          → 最深的帧为首选嫌疑
          → 返回有序候选列表
```

- pytest：`File "/path/to/foo.py", line 42, in bar`
- Java：`at com.foo.Bar.method(Bar.java:42)`，按 `src/main/java/com/foo/Bar.java` 约定还原路径

**返回候选列表而非单个文件**：最深的帧未必是 bug 所在（可能只是断言失败位置）。把有序候选交给 Detector，让模型在有排序提示的前提下自己判断。这个列表同时是 §9 评测"定位准不准"的对照基准。

### 三态判定（共享，零 LLM）

```python
def compare(baseline: FailureSet, current: FailureSet, target: str) -> Verdict:
    fixed = baseline.ids - current.ids
    new   = current.ids - baseline.ids
    if new:              return WORSE      # 引入回归——哪怕目标修好了也不收
    if target in fixed:  return BETTER
    return SAME
```

`new` 的判断在最前：**即使目标用例修好了，只要引入任何新失败一律 WORSE 并回滚**。比"净改善"更保守，但对"敢在真实 repo 上跑"是对的。

### flaky 过滤

```python
if new:
    confirmed = await rerun(new)          # 只重跑新增的那几个，不是全量
    new = new & confirmed.failed_ids      # 重跑就绿了 → 判为 flaky，不算回归
    trace.record("flaky_filtered", removed)
```

只在出现回归嫌疑时触发，且只跑那几个用例，成本近似为零，却能挡掉绝大部分因抖动导致的误回滚。被过滤的 flaky 记进 trace——顺带产出一份自己项目的 flaky 清单。

### 为什么必须写两个实现

只服务过一个实现的抽象不是抽象。写第二个会强行暴露第一个的耦合——比如上面"`classname` 不能直接重跑"，只写 pytest 时根本发现不了。第三个实现（Go / Jest）不写，留作接口稳定性的验证入口。

---

## 8. 可观测性

框架已给全套：`events.py` 十余种事件、OpenTelemetry span（`run` / `step` / `model_call` / `tool_call:{name}`）、`ModelUsage` 自带 `cost_usd` 与 `latency_ms`、`persistence/trajectory.py` 轨迹落库。本节只做**外层领域语义的接入**。

### 三层嵌套 trace

```
aifix.run                      一次调用（repo, adapter, 全局预算）
└── aifix.failure              一个失败用例（test_id）
    └── aifix.attempt          一次尝试（attempt_no）
        ├── aifix.detect ──▶ 框架 run → step → model_call
        ├── aifix.fix    ──▶ 框架 run → step → model_call / tool_call:apply_patch
        └── aifix.verify       纯代码，无子 span
```

OpenTelemetry span 天然嵌套，**app 层只要在对的位置开 span，框架那三层自动挂进来**。

### 领域属性（价值所在）

| 属性 | 位置 | 作用 |
|---|---|---|
| `aifix.verdict` | attempt | BETTER / SAME / WORSE |
| `aifix.diff_lines` | fix | 异常大意味整文件重写 |
| `aifix.rollback` | attempt | 本轮是否回滚 |
| `aifix.abort_reason` | attempt | `empty_diff` / `max_attempts` / `budget` / `loop_detected` |
| `aifix.flaky_filtered` | verify | 被抖动过滤的用例数 |
| `aifix.suspect_in_traceback` | detect | `suspect_file` 是否落在 traceback 候选里。**不是** §9 的 `locate_hit`（那个对 ground truth 判定，由评测计算）——两者是不同的集合 |
| `aifix.violation` | fix | 越界尝试，一条一行：`test_edit` / `path_escape` / `loop_abort` |

`aifix.violation` 由 §9 的评测直接取用（从 `facts.jsonl` 按类型分组计数），不必为评测单独埋点。

`aifix.suspect_in_traceback` 则**不**被 §9 取用——§9 的 `locate_hit` 是对 ground truth 判定的，两者是不同的集合。这里写死指向哪一条，是因为本表原先用「最后一条」这种位置指代，而那句话贴在了错误的指标上，直接导致评测差点量错东西。

### 三份产物（落 `.aifix/runs/<run_id>/`）

1. `events.jsonl` — 每事件一行，含每次模型调用的完整输入输出
2. SQLite 轨迹 — 复用框架 `trajectory.py`，可聚合查询
3. `report.md` — 人看的：diff、测试前后对比、每 failure 结局、成本

### 回放

```bash
aifix replay <run_id>
aifix replay <run_id> --step 7
aifix replay <run_id> --failure tests/test_auth.py::test_expired
```

agent 行为不对时，唯一有效的调试方式是看它当时到底看到了什么。这也是作品集里最直观的展示物。

### 成本

`ModelUsage.cost_usd` 聚合即可回答：本次花费、Detector/Fixer 占比、最烧钱的 failure、**平均每修好一个 bug 多少钱**。

---

## 9. 评测

### 任务集来源

| 方案 | 定位 | 说明 |
|---|---|---|
| **A. git history 挖掘** | **主力** | 自带 ground truth，分布真实 |
| B. 公开数据集 | 第二阶段 | SWE-bench Lite（Python）、Defects4J（Java，验证 MavenAdapter）；产出可比数字 |
| C. 人造变异 | 只当冒烟 | 便宜可批量，但分布与真实 bug 差距大 |

**A 的具体做法**：

```
找出让测试从红变绿的 commit C
任务 = checkout 到 C^，但保留 C 中的测试文件
期望 = agent 的补丁让该测试转绿且不引入回归
对照 = C 中的源码改动即标准答案
```

筛选可自动化：commit 同时改了源码与测试，且 checkout 到 `C^` + `C` 的测试后确实为红。

### 双档打分

```python
class TaskResult(BaseModel):
    task_id: str
    locate_hit: bool          # Detector 的 suspect_file ∈ ground truth 改动文件
    verdict: Verdict
    attempts: int
    cost_usd: float
    abort_reason: str | None
```

- **定位准确率** = `locate_hit` 占比 → Detector 的能力
- **修复成功率** = `verdict == BETTER` 占比 → 整体能力

判定复用**同一套 `compare()`**，不另写一份——否则评测在测另一个系统。

### 跨模型对比

provider 只是配置，同一批任务换 `base_url` + `model` 即可重跑：

| 模型 | 定位准确率 | 修复成功率 | 平均成本 | 平均步数 | 越界尝试 |
|---|---|---|---|---|---|

最后一列（尝试改测试文件、路径逃逸、被打转检测中止的次数）量化了**不同模型有多不听话**——正是 harness 存在的理由。

### 并行与 CI

每任务独占 worktree，天然并行（`--parallel 4`）。改完 prompt / 换完模型 / 动完工具边界后跑冒烟集（C 类，5~10 个），看成功率有无下滑。**评测集是本项目唯一的回归防护**——agent 系统无法用单元测试保证行为不退化。

---

## 10. 安全闸

### 预算：三层动态分配

```
全局预算（$2 / 500k token / 30min）—— 存 LangGraph 状态
  └── 单 failure 预算 = 全局剩余 ÷ 剩余 failure 数
        └── 单次 AgentLoop BudgetTracker
```

动态分配而非固定切分：前面省下的额度自动流给后面难的。框架在**步边界**检查，超限干净中止，不截断进行中的工具调用。

### 迭代上限：三层

| 层 | 上限 | 触发后 |
|---|---|---|
| agent 步 | `max_steps=25` | 框架中止本次 AgentLoop |
| attempt | 3 次 detect→fix→verify | 放弃该 failure，记录原因，继续下一个 |
| failure 队列 | 无上限，受全局预算约束 | — |

### 连续失败熔断

```python
if consecutive_failures >= config.consecutive_failure_limit:   # 默认 3
    abort_run("连续 3 个 failure 均未修复，疑似系统性问题")
```

连着 3 个都没修好，大概率是环境坏了 / prompt 崩了 / 模型今天不行，继续跑只是匀速烧钱。比预算上限更早生效，也更有信息量。

### 不可逆动作清单

| 动作 | 状态 |
|---|---|
| `git push` | 永不 |
| 改主工作区 | 永不（worktree 隔离） |
| 删除任何分支 | 永不 |
| 写 `.git/` | 永不（工具层拒绝） |
| 改测试文件 | 默认拒绝，`--allow-test-edits` 逃生 |
| 装依赖 / 联网 | 第一阶段不给（无 shell、无 http 工具） |
| 回滚已判 BETTER 的轮次 | 永不（只回滚未接受的改动） |

这张表能枚举，是因为工具面是白名单——"这个系统能做什么"是有限集合。

### 人在哪里：只有最终 merge

中途零人工介入。worktree 隔离让中途错误零代价（判 WORSE 就 `git checkout`），不需要人确认。**审批点越多，人越会形成看都不看点确认的肌肉记忆**。把唯一审批点放在唯一有价值的位置：最终产物。

### 失败模式总表

| # | 失败模式 | 挡在哪 | 归属 |
|---|---|---|---|
| 1 | 工具参数不符 schema | `ToolExecutor` 校验 → 喂回自纠正 | 框架 |
| 2 | tool_call 参数非法 JSON | `AgentLoop` 回填错误 → 重发 | 框架 |
| 3 | 不肯停 | `max_steps` | 框架 |
| 4 | 原地打转 | 签名窗口 → 纠偏 + 升温 → 中止 | 框架 |
| 5 | token / 时间超支 | `BudgetTracker` 步边界检查 | 框架 |
| 6 | 工具超时 / 抛异常 | `ToolError` 兜底 → `is_error` | 框架 |
| 7 | 路径逃逸 worktree | `resolve_in_workspace()` realpath | 框架 |
| 8 | 空 diff（宣称修好但没改） | `fix` 节点后置 `git diff` 检查 | app |
| 9 | 巨型 diff（整文件重写） | 同上，行数阈值 | app |
| 10 | 改测试文件蒙混过关 | `apply_patch` 路径拒绝 | app |
| 11 | 修好目标但引入回归 | 三态判定 `new` 优先 → WORSE → 回滚 | app |
| 12 | flaky 导致误判 | 新失败重跑确认 | app |
| 13 | 系统性失败持续烧钱 | 连续失败熔断 | app |

框架挡 1–7，app 挡 8–13。**这十三条就是"让 agent 靠谱"的全部具体内容** —— 就 agent 而言。

### 另一条轴：挡它、看它的那层自己会瞎

上表十三条问的都是同一个问题：**agent 会怎么失败**。还有一个问题它回答不了：**用来回答上面十三条的那套东西会怎么失败**。

这一类不编号进表 —— 它不在同一层。但它的失效形态是固定的，值得单独记：**守卫还在，只是守错了位置；计数器还在，只是永远数到 0。代码读起来完全正常。**

已经撞到三次：

| 撞在哪 | 表面 | 实际 |
|---|---|---|
| `_strip_p1`（`tools/patch.py`） | 禁改测试文件的守卫在 | 只认 `a/` `b/` 前缀，而 `git apply -p1` 剥的是**任意**第一段 —— `--- x/tests/test_add.py` 直接绕过，工具还回「补丁已应用」 |
| `count_violations`（`violations.py`） | 越界计数在 | 靠匹配上游框架的错误文案识别越界。上游改一次措辞，计数**静默归零**，而"零违规"看起来是好消息 |
| `_clip`（`replay.py`） | 「截断一定留痕」的保证在 | 按单条事件截，而流式增量一条只有几个 token —— 2000 的阈值在推理上**从未触发过**。不是被违反，是从没被执行；`--full` 对整个回放里最长的那段一直是空操作 |

三次的共同点：**没有任何一次能靠读代码发现**，因为每一处的逻辑本身都是对的，错的是它作用的对象。

所以这一类只有一种防法 —— 对抗测试：不测"守卫存在"，测"**给它一个真会触发的输入，它真的挡住了**"。

- `test_forged_path_prefix_cannot_bypass_the_test_guard` / `test_uppercase_test_dir_cannot_bypass_the_test_guard`
- `tests/test_violations.py` 里那三条 `test_sentinel_*` —— 守卫的守卫：把计数器赖以识别越界的那几条文案（两条来自上游框架、一条来自自己的工具）钉死，任何一处改了措辞就红
- `test_long_reasoning_is_clipped_to_a_single_line` —— 断言截断标记真的出现，而不只是"排版好看"

**通则：有守卫 ≠ 守卫有效。每一道守卫、每一个观测点，都要有一条测试断言它会触发。**

`eval/score.py` 开头那三条「可疑信号的已知偏差」是同源问题在评测侧的投影：那里是量错，这里是挡错，形态一样 —— 数字照常产出，看起来完全正常。

---

## 11. 项目结构与依赖

### 目录树

```
aifix-code/
├── pyproject.toml
├── src/aifix/
│   ├── cli.py              aifix run / eval / replay / mine
│   ├── config.py           AifixConfig（双模型路由、预算、阈值）
│   ├── graph.py            LangGraph 状态图 + RunState
│   ├── nodes/              preflight · baseline · detect · fix · verify · report
│   ├── agents/
│   │   ├── runner.py       consume()：AgentLoop 事件流 → AgentOutcome
│   │   ├── detector.py     提示词 + Diagnosis schema
│   │   └── fixer.py        提示词 + 工具注册
│   ├── tools/              apply_patch · run_tests · grep
│   ├── adapters/
│   │   ├── base.py         ProjectAdapter Protocol + Failure/SourceCandidate
│   │   ├── junit.py        共享：JUnit XML → FailureSet
│   │   ├── pytest_adapter.py
│   │   └── maven_adapter.py
│   ├── verify.py           三态比对 + flaky 重跑
│   ├── delivery.py         worktree · 分支 · report.md
│   └── trace.py            span 封装 + events.jsonl
├── eval/
│   ├── mine.py             从 git history 挖任务
│   ├── runner.py           并行跑任务集 + 打分
│   └── tasks/
└── tests/
```

`nodes/` 一文件一节点：节点是 trace 的单位、checkpoint 的边界、调试时唯一关心的粒度，让它们在文件系统上也一一对应。

### 依赖

```toml
dependencies = [
    "ai-harness-framework>=0.0.1",          # 已发布至 PyPI
    "langgraph>=1.2",
    "langgraph-checkpoint-sqlite>=3.1",     # SqliteSaver 在独立包里，langgraph 本体不含
]

[tool.uv.sources]                            # 本地联调时指向工作副本；不影响发布形态
ai-harness-framework = { path = "../ai-harness-framework", editable = true }
```

框架只装核心（4 个依赖），不装 `[all]`——不需要 memory / browser / mcp / docker。

`langgraph` 本体只依赖 `langgraph-checkpoint`（抽象基座），**sqlite 后端是独立包**——§4 的断点续跑必须显式加上 `langgraph-checkpoint-sqlite`，否则 `SqliteSaver` 导不进来。

`langgraph` 会带进 `langchain-core`（消息类型），属其实现细节，不使用其模型层。

### 配置

```python
class AifixConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIFIX_")

    detector: HarnessConfig                # 便宜模型
    fixer: HarnessConfig                   # 强模型
    budget_usd: float = 2.0
    budget_tokens: int = 500_000
    budget_wall_seconds: float = 1800.0    # 与 §10 的三要素预算对齐
    max_attempts: int = 3
    max_diff_lines: int = 300
    consecutive_failure_limit: int = 3
```

两条模型路由各是一个 `HarnessConfig`，复用框架配置类；`AIFIX_DETECTOR__MODEL` / `AIFIX_FIXER__MODEL` 嵌套环境变量由 pydantic-settings 原生支持。

**`allow_test_edits: bool = False` 已移除。** 本节原先列过这个字段。它从 M1 起就没有被 `src/` 里任何地方读过 —— `ApplyPatchTool._guard` 的测试文件守卫是**无条件**的，`AIFIX_ALLOW_TEST_EDITS=true` 会被正常吸收、不报错、不产生任何效果。失效方向是安全的（守卫照拦，fails closed），但它是一个接受你的输入然后什么都不做的旋钮。

**不接线而是删掉**，理由是本规格自己的核心主张：只有零 LLM 的确定性代码有资格说「修好了」，而**测试就是那个 oracle**。允许 agent 改测试等于允许它改判卷标准 —— M3 的真实验收里模型把 `add` 改成有状态函数去满足一个自相矛盾的断言，已经证明了它有多想走这条路。一个没接线的危险旋钮比没有旋钮更糟：它给人「需要时可以打开」的错觉。真要开这个口子，那是一次需要认真设计的改动 —— 至少要有启动时的响亮警告、trace 里的显式记录、报告里的红字标注、评测里单独一列 —— 而不是把一个 bool 接上去。

（`model_config` 用 `extra="ignore"`，所以删除之后 `AIFIX_ALLOW_TEST_EDITS` 仍会被静默吸收。这是有意的：改成 `extra="forbid"`，上游多设一个 `AIFIX_` 开头的环境变量就会让所有人启动失败。代价是拼错的配置名同样不报错。）

### CLI

```bash
aifix run                          # 当前 repo，修所有失败用例
aifix run --test tests/x.py::test_y --budget 0.5
aifix run --dry-run                # 只跑 preflight + baseline，报告有多少活

aifix eval --suite eval/tasks --model glm-4.6 --parallel 4
aifix mine --since 6months --out eval/tasks
aifix replay <run_id> [--step N]
```

---

## 12. 对 ai-harness-framework 的改动

本项目对框架的唯一需求，且都向后兼容：

### 12.1 `LocalSandbox` 支持接管既有目录

```python
class LocalSandbox:
    def __init__(self, workspace: str | None = None) -> None:
        self._adopted = workspace is not None      # 接管既有目录：close() 不删
        self.workspace = workspace or ""
```

`LocalSandbox()` 行为完全不变。建 worktree 的逻辑留在 aifix 的 `delivery.py`——**框架不需要知道 git 的存在**。

**威胁模型说明**：`LocalSandbox` 无进程隔离，`exec` 以当前用户权限在宿主机运行。本项目的威胁模型是"糊涂的 agent 弄坏东西"而非"恶意代码逃逸"；对前者，路径夹紧 + worktree 隔离 + 无 shell 已足够。真需进程隔离应换 `DockerSandbox`，代价是容器内需装齐目标 repo 的工具链——列为可选项，非默认。

### 12.2 `AgentLoop.run()` 支持传入完整初始消息

现签名只收 `user_message: str`。Fixer 需要带着 Detector 的诊断与结构化上下文进场。扩展为可选参数，保留旧用法：

```python
async def run(self, user_message: str | None = None, *,
              messages: list[Message] | None = None) -> AsyncIterator[Event]: ...
```

两项均需补测试，且 ai-learning-helper 的完整测试套件仍须全绿。

---

## 13. 分阶段范围

### 第一阶段（本规格的实现范围）

内容量超出单份实现计划的覆盖范围，拆为三个里程碑，**每个里程碑对应一份独立的实现计划**，且各自都是可用的产物：

**M1 — 端到端最小闭环**
`preflight → baseline → detect → fix → verify → report` 全链路打通；只做 `PytestAdapter`；5 个工具；三态判定；worktree 交付；框架侧两处改动（§12）。
*验收*：在一个真实的 pytest 项目上，红色测试进去、绿色分支出来，主工作区未被触碰。

**M2 — 靠谱**
补齐十三条安全闸中属于 app 的六条（空 diff / 巨型 diff / 改测试文件 / 回归回滚 / flaky 过滤 / 连续失败熔断）；三层预算动态分配；三层嵌套 trace + `events.jsonl` + `report.md`。
*验收*：M1 的闭环在异常输入下不崩、不越界、不烧穿预算，且每一步可从 trace 复盘。

**M3 — 可度量**
`MavenAdapter`（验证适配层抽象）；`aifix mine` 从 git history 挖任务集；`aifix eval` 并行跑 + 双档打分；`aifix replay`。
*验收*：跑出第一张跨模型对比表（定位准确率 / 修复成功率 / 平均成本 / 越界尝试）。

顺序不可调换：M2 依赖 M1 的闭环存在，M3 的评测依赖 M2 的判定与 trace 已稳定。

### 第二阶段（不在本规格内）

- **任务/issue 驱动**：输入变为自然语言，系统先写复现测试（必须先红后绿），再走同一条 detect→fix→verify 流程
- SWE-bench Lite / Defects4J 对外可比数字
- 自动开 PR（`gh pr create`，等价于当前交付形态加一步）
- 第三个 `ProjectAdapter` 实现（Go / Jest），验证接口稳定性

### 明确不做

Web UI · Reviewer agent · 主动扫描驱动 · 自动 merge。
