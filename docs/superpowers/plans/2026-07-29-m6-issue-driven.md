# M6「issue 驱动」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development 逐任务实现此计划。

**目标：** 在 issue 里评论一句 `/aifix`，GitHub Actions 自动读懂 issue、写出一条**复现测试**、走现有核心循环把它从红修到绿，最后开一个 PR。人只在 PR 上审一次。

**架构：** 一块真功能 + 两层胶水。真功能是 `reproducer`——把一段人话变成一条可执行的红测试，它是这个里程碑唯一的新能力，也是唯一的不确定来源。两层胶水分别是 issue 事件的进出（解析、授权、回帖、开 PR）与 Actions 接线。**核心循环 `run_once` 一行不改**：复现测试先 commit 进 HEAD，随后它就是队列里唯一的目标用例，走的还是 M1 那条路。

**技术栈：** Python 3.14 · GitHub Actions（`ubuntu-latest`）· `gh` CLI（runner 自带）· uv

**规格：** 折在本文档「设计决策」一节里。M6 的取舍全部来自一次完整的设计讨论，没有单独的规格文档——**若实现过程中推翻了任何一条决策，就地更正这份文档并写明为什么，不要悄悄偏离。**

---

## 全局约束

1. **提交署名**：一律 `git -c user.name=sumengnan -c user.email=2499165351@qq.com commit -m "..." -- <paths>`。**必须带 pathspec**——裸 `git commit` 会提交整个 index，并发时会卷进别人的文件（本项目发生过一次）。commit message 中**绝不出现** AI / Claude / Anthropic / `Co-Authored-By` 字样。
2. **严格 TDD**：先写失败测试 → 跑一次确认失败并把真实输出记进报告 → 实现 → 跑通 → commit。
3. **断言必须有区分度**。本项目已有**六次以上**恒真断言教训：`"0%" in "100%"` 恒真；`assert cost > 0`；整体替换被 patch 的函数导致代码提前返回、断言恒真却一直绿；`for x in empty: assert ...` 无条件通过。
4. **不自造第三方的产出**。断言 GitHub 事件载荷形状的测试，其 fixture 必须来自**真实投递**（从仓库 Settings → Webhooks 的 Recent Deliveries 里抄，或 `gh api` 拉一条真 issue）。手写的 JSON 只能证明我们理解得自洽。
5. **凡是判定，零 LLM**。授权、命令解析、红检、交付通路选择，全部由确定性代码做。模型只负责生成复现测试。
6. 注释写「为什么」不写「是什么」，中文。
7. 全量套件 592 项、378–726 秒（同机波动近一倍）。只在任务要求时跑。

---

## 设计决策

十几轮讨论收敛下来的七条。每条都写了**为什么**，因为它们中有几条看起来像是可以随便换的。

**1. 只有一道人闸，在最终 PR 上。**
中途不停下来等签字。理由：issue 由仓库主自己提，"这条复现测试对不对"他在 PR 里连同补丁一起看得出来；而中间闸要付出的代价是整条流水线从"一次 run 跑完"变成"跨 run 的状态机"（要 `state.json`、要断点、要超时清理）。等到**别人的 issue 也能触发**时再上中间闸，那时候复现测试的内容不再由他决定，事前签字才有意义。

**2. 触发限制在仓库主自己的 issue 上。**
判据两条同时成立：评论者 `author_association == 'OWNER'`，且 `issue.user.login == repository_owner`。
第二条是关键——它把**提示注入面归零**。模型读到的每个字都是仓库主自己写的。只限制触发者而不限制 issue 作者的话，攻击路径是「外人提一个藏了指令的 issue，等仓库主去评论 `/aifix`」，而仓库主本来就想修 bug，那一步门槛低得可怜。

**3. reproducer 无写入权，只输出 JSON。**
它拿到 `read_file` / `grep` 两个工具（不给 `apply_patch`），最终产出一段 JSON，**由确定性代码把测试写进文件**。
这条顺带解决了守卫问题：现有的"不许改测试文件"守卫查的是 **agent 的工具调用**，而这里写文件的是 aifix 自己，压根不经过工具面。**所以守卫一行都不用改，也不需要冻结哈希**——修复阶段的 agent 依然被原封不动地挡在测试文件之外。任务 2 要写一个测试把这件事钉死。

**4. 复现测试先 commit 进 HEAD，再走 `run_once`。**
不新建"issue 分支"，不改 `run_once` 的签名。顺序是：在 runner 的本地 checkout 上 commit 复现测试 → `run_once` 从 HEAD 建 worktree（自然包含它）→ baseline 把它认成一个失败用例 → `only_test` 把队列削成只有它 → 现有循环照跑。
交付分支 `aifix/<run_id>` 上因此天然含有两个 commit：复现测试、修复。PR 的 diff 一屏看完。

**5. 交付分三条通路（本次头脑风暴确认）。**

| 情形 | 产出 |
|---|---|
| 写不出复现测试 | **只回帖**，列出 issue 缺哪些信息。不建分支、不开 PR |
| 写出了复现、没修好 | **照样开 PR**，标题标明「复现已就位，未修复」 |
| 修好了 | 开 PR，报告写进 body |

第二条的理由：一条红着的复现测试**本身就是产出**，人可以直接接手。丢掉它等于丢掉这次 run 里唯一有价值的东西。
第一条的理由：写不出复现说明 issue 信息不足，那是一条 triage 结论，不是代码产出——没有分支可交付。而且它极便宜（一次模型调用，fixer 完全不启动），**即使一个 bug 都修不成，这个能力本身就值**。

**6. trace 两处留存（本次头脑风暴确认）。**
- `.aifix/runs/<run_id>/` 全量（含体积大的 `events.jsonl`）→ `upload-artifact`，90 天，供 `replay`
- `facts.jsonl` + `report.md` → **`aifix/traces` 孤儿分支**，永久，供 `ingest` / `stats`

这个划分正好是 `trace.py:7-8` 里已经写下的那条区分：**事实是结论，事件是原始素材**。前者要长期统计所以要永久，后者只在出问题时才要所以 90 天够。
必须做的原因：runner 是临时的，不主动持久化就**全部消失**；而 `ingest` 现在扫的是 `<repo>/.aifix/runs/*/`，它假设多次 run 的产物堆在同一个目录里——在 Actions 上那个目录下永远只有一个 run，跨 run 汇总天然失效。

**7. reproducer 复用 `fixer` 的模型路由，不新增配置。**
写复现测试要读代码、理解语义、拼出正确的 import 和调用签名，量级接近 `fixer` 而不是 `detector`（后者是单步、无工具、强制 JSON 的诊断）。
什么时候该拆出第三条路由：当实测发现"便宜模型写复现测试也够用"时。**在有数据之前加这个配置是 YAGNI**，而且多一条路由就多一处「配了但没生效」的失败面。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/aifix/agents/reproducer.py` | **新建** | 系统提示、prompt 构造、JSON 解析。形制照 `agents/detector.py` |
| `src/aifix/reproduce.py` | **新建** | `reproduce`（带只读工具的 AgentLoop）+ `red_check`（零 LLM）+ 落盘 |
| `src/aifix/issue/event.py` | **新建** | 读 `$GITHUB_EVENT_PATH`，解析成 `IssueEvent`；命令解析；授权判定 |
| `src/aifix/issue/github.py` | **新建** | 回帖、编辑状态评论、开 PR。薄壳，走 `gh` CLI |
| `src/aifix/issue/handle.py` | **新建** | 编排：event → reproduce → `run_once` → 三条交付通路 |
| `src/aifix/cli.py` | 命令行 | 改：加 `aifix reproduce` 与 `aifix issue handle` 两个子命令 |
| `src/aifix/nodes/preflight.py` | 前置校验 | 改：加模型可达性预检 |
| `src/aifix/trajectory.py` | 跨 run 轨迹 | 改：`ingest` 支持指定 runs 目录 |
| `.github/workflows/aifix.yml` | **新建** | 薄壳，只调 `aifix issue handle` |

`config.py` **不动**——决策 7 说明了为什么不加 reproducer 路由。

---

## 阶段一 · reproducer（完全本地，不碰 GitHub）

这一阶段结束时你就知道这条路走不走得通了。**它是整个里程碑的风险所在**，后面两个阶段都是胶水。

### 任务 1：`reproducer` agent

**背景**：输入是 issue 标题 + 正文，输出是一条复现测试的**源码**。它必须能读代码——零上下文生成的测试连 import 都写不对。

产出的 JSON 契约：

```json
{
  "can_reproduce": true,
  "test_file": "tests/test_issue_42.py",
  "test_code": "def test_export_csv_missing_column():\n    ...",
  "target_test_id": "tests/test_issue_42.py::test_export_csv_missing_column",
  "missing_info": []
}
```

`can_reproduce: false` 时 `missing_info` 必须非空——那是要回帖给人看的 triage 结论（缺复现步骤 / 缺环境 / 缺期望行为）。

**文件：** 新建 `src/aifix/agents/reproducer.py`；测试 `tests/test_reproducer.py`

- [ ] **步骤 1：写失败测试**

```python
def test_parse_rejects_reproducible_claim_without_a_target_id():
    """说能复现却不给 target_test_id —— 下游没有用例可跑，必须当解析失败。

    反向对照：这条测试存在的理由是「模型少给一个字段」不能被读成
    「复现成功」。少了它，下游会拿着 None 去跑 scoped 测试，pytest
    收集不到任何用例、退出码为 5，而那个形态和「测试红了」区分不开。
    """
    assert parse_reproduction('{"can_reproduce": true, "test_code": "x"}') is None


def test_parse_rejects_failure_claim_without_missing_info():
    """说不能复现却不说缺什么 —— 回帖会是一句没有信息的废话。"""
    assert parse_reproduction('{"can_reproduce": false, "missing_info": []}') is None


def test_parse_accepts_a_complete_reproduction():
    r = parse_reproduction(json.dumps({
        "can_reproduce": True, "test_file": "tests/test_x.py",
        "test_code": "def test_x(): assert False",
        "target_test_id": "tests/test_x.py::test_x", "missing_info": []}))
    assert r is not None and r.target_test_id == "tests/test_x.py::test_x"


def test_parse_rejects_a_test_file_outside_the_test_dirs():
    """路径逃逸与「把测试写进 src/」都要挡 —— 写进产品目录等于让它绕开
    「不许改测试文件」的整套前提。"""
    for bad in ("../evil.py", "/etc/passwd", "src/aifix/cli.py"):
        assert parse_reproduction(json.dumps({
            "can_reproduce": True, "test_file": bad, "test_code": "x",
            "target_test_id": f"{bad}::t", "missing_info": []})) is None
```

- [ ] **步骤 2：跑一次确认失败**，把真实输出记进报告
- [ ] **步骤 3：实现** `SYSTEM_PROMPT` / `build_prompt(issue_title, issue_body, adapter)` / `parse_reproduction(text)`
- [ ] **步骤 4：跑通，commit**

系统提示里必须写死的三条：只产出一条测试函数；**不许修改任何已有文件**；测试必须针对 issue 描述的行为断言，不许写恒真断言。

### 任务 2：`reproduce` + `red_check`

> **实现时的更正**：原表把它放在 `src/aifix/nodes/reproduce.py`。**它不是 LangGraph 节点**——图的入口是 `run_once`，而按决策 4，复现必须发生在 `run_once` 之前（测试要先进 HEAD）。放进 `nodes/` 会让人以为它是 `build_graph()` 装配的一环。已改为顶层 `src/aifix/reproduce.py`。

**背景**：`reproduce` 是带只读工具的 AgentLoop（`read_file` / `list_files` / `grep`，**不给 `apply_patch`，也不给 `run_tests`**），拿到 JSON 后由确定性代码落盘。不给 `run_tests` 的理由和不给 `apply_patch` 同级：让模型自己跑测试，「这条测试红不红」的判定权就落到了它手里，而红检是这一步唯一的确定性证据。`red_check` 零 LLM，判两件事：

1. 目标用例**必须红**
2. 红的形态**不能是收集错误**——`ImportError` 说明模型猜错了模块，它红得没有信息量。这一类要打回，不能当成"复现成功"

`baseline.py` 里已有收集错误的判别逻辑，复用，不要另写一份（本项目在"第二份注册表"上栽过）。

**文件：** 新建 `src/aifix/nodes/reproduce.py`；测试 `tests/test_reproduce_node.py`

- [ ] **步骤 1：写失败测试**

```python
async def test_red_check_rejects_a_test_that_passes(tmp_repo):
    """在 HEAD 上就绿的测试约束力为零 —— 必须打回，且不能惊动人。"""
    ok, reason = await red_check(tmp_repo, adapter, target_id_of_a_passing_test)
    assert ok is False and "没有失败" in reason


async def test_red_check_rejects_a_collection_error(tmp_repo):
    """红得空：import 不到东西也是红，但它复现的是模型自己的笔误。"""
    ok, reason = await red_check(tmp_repo, adapter, target_id_that_import_errors)
    assert ok is False and "收集" in reason


async def test_red_check_accepts_a_genuine_assertion_failure(tmp_repo):
    ok, _ = await red_check(tmp_repo, adapter, target_id_that_asserts_wrong)
    assert ok is True


async def test_reproducer_registry_has_no_write_tools():
    """决策 3 的钉子：reproducer 一旦拿到 apply_patch，「守卫不用改」这条
    前提就不成立了 —— 它能直接改产品代码，而那条路径上没有任何守卫。
    """
    names = {t.name for t in build_reproduce_registry(sandbox, adapter).tools}
    assert "apply_patch" not in names and "run_tests" not in names
```

- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

### 任务 3：`aifix reproduce` 子命令 —— 第一次撞真实

```bash
uv run aifix reproduce . --issue-text issue.md
```

跑完打印：能不能复现、写出了什么、红检过没过、红的形态是什么。**不修任何东西**，就到复现为止。

**这是整个计划里第一个能给出真实读数的地方。** 拿三到五个你自己仓库里真实的、已经修过的 bug，把当初的 commit message 当 issue 正文喂进去，看它写出来的测试对不对。

- [ ] **步骤 1：写失败测试**（子命令存在、`--issue-text` 缺失时报错、退出码语义）
- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**
- [ ] **步骤 5：真实验收** —— 五个真 bug，把「几个能写出复现、几个红得对」记进报告

> **验收点**：如果五个里少于两个能写出红得对的复现，**停下来先改 prompt 和上下文构造**，别急着往阶段二走。后面两个阶段全是胶水，胶水不会让这个数字变好。

---

## 阶段二 · issue 胶水（用假 event JSON，仍不碰 GitHub）

### 任务 4：事件解析、命令解析、授权判定

**背景**：这一层全部是确定性代码，且必须**本地可测**——`issue_comment` 的 workflow 文件必须在默认分支上才会被触发，靠真实评论调试会慢到不可接受。

判据（全部同时成立才放行）：

| 判据 | 值 |
|---|---|
| 事件类型 | `issue_comment` 且 `action == 'created'`（**不接 `edited`**） |
| 命令 | 评论**第一行**精确匹配 `/aifix`（不在全文里搜） |
| 评论者 | `author_association == 'OWNER'` |
| issue 作者 | `issue.user.login == repository_owner` |
| 不是 bot | `comment.user.type != 'Bot'` |

**文件：** 新建 `src/aifix/issue/event.py`；测试 `tests/test_issue_event.py`。fixture 用**真实投递**的 JSON（约束 4）。

- [ ] **步骤 1：写失败测试**

```python
def test_edited_comments_are_ignored(real_payload):
    """只认 created：否则一条三个月前的旧评论被编辑成 /aifix 就能触发。"""
    p = {**real_payload, "action": "edited"}
    assert authorize(p) .allowed is False


def test_command_must_be_on_the_first_line(real_payload):
    """全文搜索会把引用别人的话、或正文里贴的一段命令当成指令。"""
    p = deep_set(real_payload, "comment.body", "他说：\n/aifix\n我觉得不用")
    assert authorize(p).allowed is False


def test_outsider_issue_is_rejected_even_when_owner_comments(real_payload):
    """决策 2 的钉子：只限制触发者挡不住注入 —— 外人提 issue，仓库主
    顺手打上触发命令，模型读到的仍然是外人写的字。
    """
    p = deep_set(real_payload, "issue.user.login", "someone-else")
    assert authorize(p).allowed is False


def test_rejection_always_carries_a_human_readable_reason(real_payload):
    """拒绝必须能回帖说明。静默丢弃会让人以为批过了 —— 本项目栽过十次
    以上的正是「不报错，只有承诺是假的」。
    """
    p = deep_set(real_payload, "comment.author_association", "CONTRIBUTOR")
    d = authorize(p)
    assert d.allowed is False and d.reason
```

- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

### 任务 5：GitHub 输出侧

三个动作：创建/编辑**一条**状态评论（不刷屏）、开 PR、给评论加 reaction 作即时回执。

走 `gh` CLI（runner 自带，省掉一个 HTTP 客户端依赖）。测试用假的 `gh` 可执行文件（`tmp_path` 里放一个记录参数的脚本），**断言命令行拼得对**，不打真实网络。

**关键细节：PR 必须用 `GITHUB_TOKEN` 开。** 这样作者是 `github-actions[bot]`，仓库主才能 approve——GitHub 不允许批准自己开的 PR。

**文件：** 新建 `src/aifix/issue/github.py`；测试 `tests/test_issue_github.py`

- [ ] **步骤 1：写失败测试**（含：状态评论是**编辑**不是新建；PR body 来自 `report.md`；未修复时标题带标记）
- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

### 任务 6：`aifix issue handle` 编排

```
读 event → 授权（不过就回帖说明并退出 0）
  → 👀 reaction
  → reproduce → red_check
      ├─ 写不出 → 回帖列出 missing_info，退出 0        ← 通路一
      └─ 写出了 → git commit 复现测试（本地）
  → run_once(repo=".", only_test=target_test_id)
  → git push aifix/<run_id> → 开 PR                    ← 通路二/三
  → 编辑状态评论：结果、成本、可疑信号、artifact 链接
```

三处必须做对：

1. **baseline 里有别的红时不中止，但要在 PR body 里出声。** 那多半是环境差异（runner 的镜像会漂移），而它会污染"这个补丁没弄坏别的"这个判断。硬中止会因为一个抖动的用例把整次 run 毁掉，所以只出声。
2. **`run_once` 的异常已经保证报告先落地**（`cli.py:208-222`）。编排层不要再包一层 try 把它吞掉——直接用 `state["abort_kind"]` 区分通路。
3. **退出码**：授权不过 = 0（不是错误，是正常拒绝）；写不出复现 = 0；没修好 = 0（PR 开出来了，是有效产出）；崩溃 = 1。**只有崩溃才让 job 红**——否则 Actions 页面会满屏红叉，而其中大半是正常结论。

**文件：** 新建 `src/aifix/issue/handle.py`；测试 `tests/test_issue_handle.py`（打桩 `run_once` 与 `reproduce_node`，只测编排与通路选择）

- [ ] **步骤 1：写失败测试**（三条通路各一个；退出码各一个）
- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

---

## 阶段三 · Actions 接线

### 任务 7：模型可达性预检

**背景**：不预检的话，模型连不上的表现是——每一轮都修不好 → 重试三次 → 熔断 → 报告写「连续 N 个 failure 均未修复，疑似系统性问题」。**跑了几十分钟，最后给出一句指错方向的诊断。**

这与 `preflight.py:18-21` 已经写下的那段注释是同一个形状：

> 必须在这里拦住而不是留给 baseline……这个项目里最贵的失败一向不是崩溃，是指错方向的诊断。

在 preflight 里发一次极小的调用（几十 token），失败就当场中止并说明是网络或凭据问题。Actions 上比本地更值——本地能立刻看到报错，runner 上要等它跑完才知道。

**文件：** 改 `src/aifix/nodes/preflight.py`；测试 `tests/test_nodes_preflight_baseline.py`

- [ ] **步骤 1：写失败测试**（打桩 client 抛连接错误 → `abort` 里出现凭据/网络字样，且**不出现**"模型没修好"这类措辞）
- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

### 任务 8：trace 持久化

两件事：

**(a) `ingest` 支持指定 runs 目录。** 现在写死扫 `<repo>/.aifix/runs/`（`trajectory.py:205`）。加一个参数，这样 clone 下 traces 分支就能直接灌库。

**(b) traces 孤儿分支。** 每次 run 结束往 `aifix/traces` 追加 `runs/<run_id>/facts.jsonl` + `report.md`。**不含 `events.jsonl`**（体积大，artifact 里已有）。

注意 `.aifix/` 在 `.gitignore` 里，所以往孤儿分支提交时要显式 `git add -f`。

**文件：** 改 `src/aifix/trajectory.py`、`src/aifix/cli.py`；测试 `tests/test_trajectory.py`

- [ ] **步骤 1：写失败测试**（`ingest` 指定目录后能读到那份 facts；孤儿分支上的目录布局能被 `ingest` 认出）
- [ ] **步骤 2：跑一次确认失败**
- [ ] **步骤 3：实现**
- [ ] **步骤 4：跑通，commit**

### 任务 9：workflow 文件

```yaml
# .github/workflows/aifix.yml —— 必须在默认分支上才会被触发
name: aifix
on:
  issue_comment:
    types: [created]

concurrency:
  group: aifix-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  fix:
    if: |
      github.event.comment.author_association == 'OWNER' &&
      github.event.issue.user.login == github.repository_owner &&
      startsWith(github.event.comment.body, '/aifix')
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: write
      issues: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync

      - run: uv run aifix issue handle
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AIFIX_FIXER__MODEL: deepseek-v4-pro
          AIFIX_FIXER__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_FIXER__API_KEY: ${{ secrets.AIFIX_API_KEY }}
          AIFIX_DETECTOR__MODEL: deepseek-v4-flash
          AIFIX_DETECTOR__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_DETECTOR__API_KEY: ${{ secrets.AIFIX_API_KEY }}
          AIFIX_PRICE_MAP: ${{ vars.AIFIX_PRICE_MAP }}
          AIFIX_TEST_PYTHON: ${{ github.workspace }}/.venv/bin/python
          AIFIX_BUDGET_USD: "0.50"
          AIFIX_BUDGET_WALL_SECONDS: "3600"

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: aifix-trace-${{ github.event.issue.number }}
          path: .aifix/runs/
```

`if:` 里那三条判据是**便宜的前置过滤**（绝大多数评论事件在这里就被挡掉，一秒 runner 都不花），任务 4 里那套是**权威判定**。两处都要，不是重复——workflow 的 `if:` 拦不住的（比如 `edited`、比如命令不在第一行），由代码拦。

三处刻意的取值：

- **`AIFIX_TEST_PYTHON` 显式配**，不靠探测。runner 上 clone 出来没有 `.venv`，探测落空会退回 aifix 自己的解释器，然后就是那个「11 个 collection error」的场景（见 `fix(eval): 评测照着克隆探解释器`）。
- **`AIFIX_BUDGET_WALL_SECONDS` (3600) 必须显著小于 `timeout-minutes` (90)。** Actions 的超时是**杀进程**，`run_once` 那个保证报告先落地的 except 分支根本执行不到——跑了一小时，什么都没留下。让软闸先响。
- **`AIFIX_PRICE_MAP` 用 variable 不用 secret。** 价格表不是机密，放 secret 里会被日志遮蔽成 `***`，你反而看不出它配没配对——**而没配价格表的后果是美元闸永远不触发**。

- [ ] **步骤 1：连通性先验证**（**在写这个文件之前做**）

```yaml
- run: curl -sS -o /dev/null -w "%{http_code}\n" $BASE_URL/models -H "Authorization: Bearer $KEY"
```

runner 的出口 IP 是 Azure 的动态大段。**你的模型 endpoint 若有 IP 白名单，整条 Actions 路线要推倒重来**——不值得等胶水全写完才发现。

- [ ] **步骤 2：写 workflow，合进默认分支**
- [ ] **步骤 3：真实 issue 端到端验收**

### 任务 10：整体验收

- [ ] 自己提一个真实 issue（描述一个真 bug），评论 `/aifix`
- [ ] 确认：状态评论出现 → PR 开出来 → diff 里有复现测试和补丁两个 commit
- [ ] 确认：artifact 能下载，`aifix replay` 能读
- [ ] 确认：`aifix/traces` 分支上有这次的 facts，clone 下来 `aifix ingest` 能灌库
- [ ] **故意跑一次注定修不好的**，确认走通路二（PR 开出来且标明未修复）
- [ ] **故意提一个信息不全的 issue**，确认走通路一（只回帖，不建分支）
- [ ] 全量套件绿，把耗时读数记进报告
- [ ] 更新 README：命令一览加 `aifix reproduce` / `aifix issue handle`，项目状态更新测试数

---

## 顺序与依赖

```
阶段一（任务 1→2→3）  ← 唯一的真功能，也是唯一的风险
      ↓  验收点：五个真 bug 里几个能写出红得对的复现
阶段二（任务 4、5 可并行 → 任务 6）
      ↓
阶段三（任务 7、8 可并行 → 任务 9 → 任务 10）
```

任务 7 和 8 与前两个阶段无依赖，赶时间的话可以提前插。

---

## 交给后续

明确不在 M6 内，且都是**有意留的**：

- **中间人审闸**（决策 1）。等别人的 issue 也能触发时再上，那时候要的形态是 PR review 触发续跑，不是 issue 评论。
- **公开给外人用**。注入面在决策 2 里是靠"只处理仓库主自己的 issue"归零的。要放开，得先做「执行与权限分离」——跑测试的 job 不给任何 secret。
- **复现测试准确率的离线评测**。拿 `evals/` 里那 39 个任务、只喂 commit message、藏掉真实测试来量。本次头脑风暴选了直接建端到端，所以这条挪到 M6 之后——**但任务 3 的验收点会给出一个小样本读数，别忽略它**。
- **别的仓库接入**。要做的话是发一个 Action，不是托管服务：用户自己的 CI 已经解决了目标仓库的测试环境，那正是接入最难的一块。
- **Actions 之外的平台**（Gitee 等）。第二个平台真要接的时候再抽接口——`MavenAdapter` 那六处裂缝的教训是，只有一个实现的抽象分不清是对的还是恰好长得像那一个实现。
