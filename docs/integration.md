# 接入教程：把 aifix 接到你自己的项目上

这份文档讲的是**别人的项目怎么用上 aifix** —— 在你自己的仓库里开一个 `/aifix` 开头的
issue，让它读懂描述、写复现测试、修、开 PR。

（如果你想看的是 aifix 这个仓库自己怎么配，那是 [issue-driven.md](issue-driven.md)。）

**预计耗时**：连通性自检 5 分钟 + 配置 20 分钟 + 第一次真跑 10~30 分钟。

---

## 目录

- [第 0 步：五分钟判断能不能用](#第-0-步五分钟判断能不能用)
- [第 1 步：连通性自检](#第-1-步连通性自检)
- [第 2 步：本地先跑一次（强烈建议）](#第-2-步本地先跑一次强烈建议)
- [第 3 步：把 workflow 放进你的仓库](#第-3-步把-workflow-放进你的仓库)
  - [Python / pytest 项目](#python--pytest-项目完整可复制)
  - [Java / Maven 项目](#java--maven-项目完整可复制)
- [第 4 步：配 secrets 和 variables](#第-4-步配-secrets-和-variables)
- [第 5 步：仓库设置里那两格](#第-5-步仓库设置里那两格)
- [第 6 步：第一次真跑与验收清单](#第-6-步第一次真跑与验收清单)
- [授权模型：谁能触发，怎么给别人开权限](#授权模型谁能触发怎么给别人开权限)
- [成本：这一下大概花多少钱](#成本这一下大概花多少钱)
- [接入时最容易踩的七个坑](#接入时最容易踩的七个坑)
- [不接 GitHub，只在本地用](#不接-github只在本地用)

---

## 第 0 步：五分钟判断能不能用

对着下面这张表勾一遍。**任何一条是「否」，先别往下走。**

| # | 条件 | 为什么 |
|---|---|---|
| 1 | 项目是 **pytest** 或 **Maven** 工程 | 目前只有这两个适配器。别的体系要先写一个，见 [adapters.md](adapters.md#怎么加第三个适配器) |
| 2 | 你对这个仓库有**写入权限**（所有者 / 协作者 / 组织成员） | 触发权 = 已经能改这个仓库的人。别人要用得先开权限，见[授权模型](#授权模型谁能触发怎么给别人开权限) |
| 3 | 你有一个 **OpenAI 兼容**的模型端点（base_url + api_key） | aifix 走 `/v1/chat/completions` |
| 4 | 那个端点**没有 IP 白名单** | GitHub runner 的出口 IP 是 Azure 的动态大段。这一条做不成整条 Actions 路线就不成立 —— 所以它是第 1 步 |
| 5 | 你的测试套件在 CI 上能跑起来，且**单次全量在 10 分钟以内** | 一次 run 要跑 1 次 baseline + 每轮 1 次 verify。20 分钟的套件意味着一次 run 一小时起步 |
| 6 | 你的测试**基本不抖** | 抖动会被过滤，但 baseline 里本来就红的用例会稀释「这个补丁没弄坏别的」这个判断 |
| 7 | 你能接受**每次触发花 ¥1 ~ ¥15** | 见[成本那一节](#成本这一下大概花多少钱) |

第 5 条不满足也不是完全没戏 —— 调大 `AIFIX_TEST_TIMEOUT_SECONDS` 和 job 的
`timeout-minutes` 能跑，只是慢且贵。

**第 5 条也是最容易改善的一条**：目标项目装上 `pytest-xdist`，aifix 会自动并行跑全量
（实测本仓库 956 个用例：7 分 12 秒 → 3 分 58 秒，一次 run 省 9 分钟）。前提是你的套件
xdist-安全 —— 不安全就设 `AIFIX_TEST_PARALLEL=off`，详见
[configuration.md](configuration.md#aifix_test_parallel)。

---

## 第 1 步：连通性自检

**这是接 aifix 之前唯一一件做不成就得推倒重来的事。** runner 的出口 IP 是动态的，端点
若有 IP 白名单，整条路线不成立 —— 而这个事实在写完全部胶水之后才发现的代价是几天。

把下面这个文件放进你的仓库 `.github/workflows/aifix-connectivity.yml`，然后
`gh workflow run aifix-connectivity.yml`：

```yaml
name: aifix-connectivity

on:
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions: {}          # 只往外打 HTTP，不碰仓库

    steps:
      - name: runner 的出口 IP（拿去比对你的白名单）
        run: echo "出口 IP：$(curl -sS --max-time 10 https://api.ipify.org || echo '取不到')"

      - name: 端点可达性 + 凭据
        env:
          BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          KEY: ${{ secrets.AIFIX_API_KEY }}
          MODEL: ${{ vars.AIFIX_FIXER__MODEL || 'qwen3-coder-flash' }}
        run: |
          set -u
          if [ -z "${BASE_URL:-}" ] || [ -z "${KEY:-}" ]; then
            echo "::error::secrets.AIFIX_BASE_URL 或 secrets.AIFIX_API_KEY 没配"; exit 1
          fi

          # 真发一次最小的 chat completion —— 这才是 aifix 实际走的那条路。
          # /models 通不代表 chat 通（鉴权范围、模型名、配额都可能只在这里炸）。
          code=$(curl -sS --max-time 60 -o /tmp/chat.json -w '%{http_code}' \
                 "$BASE_URL/chat/completions" \
                 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
                 -d "{\"model\":\"$MODEL\",\"max_tokens\":1,
                      \"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}" || echo 000)
          echo "POST /chat/completions → HTTP $code"
          head -c 600 /tmp/chat.json 2>/dev/null || true; echo

          case "$code" in
            2*)  echo "✅ 端点可达、凭据有效、模型名正确 —— Actions 这条路走得通" ;;
            000) echo "::error::连不上（超时或 DNS）。最可能是端点有 IP 白名单，"\
                      "而 runner 的出口 IP 是动态的 —— 见上面那行出口 IP"; exit 1 ;;
            401|403) echo "::error::凭据被拒。key 错了，或这个 key 没有该模型的权限"; exit 1 ;;
            404) echo "::error::路径或模型名不对。BASE_URL 要以 /v1 结尾"; exit 1 ;;
            *)   echo "::error::HTTP $code，看上面的响应体"; exit 1 ;;
          esac
```

（要跑它得先做完[第 4 步](#第-4-步配-secrets-和-variables)的两个 secret。）

---

## 第 2 步：本地先跑一次（强烈建议）

在 Actions 上调试的反馈周期是几分钟一轮，而且这两个事件的 workflow **只从默认分支加载**
—— 每改一次都要合一次。本地能跑通的东西，别拿到 CI 上去调。

```bash
# 1. 装 aifix。**千万别装进你项目的 venv** —— 它自己依赖 langgraph、pydantic、
#    openai，混进去可能起冲突，更糟的是污染跑 baseline 的那套环境。
#    uv tool install 天然装在隔离环境里；发行名是 aifix-code，命令叫 aifix。
uv tool install aifix-code
# 没有 uv 就用这两行：
# python -m venv /tmp/aifix-venv && /tmp/aifix-venv/bin/pip install aifix-code

# 2. 配模型
export AIFIX_FIXER__BASE_URL="https://your-endpoint/v1"
export AIFIX_FIXER__API_KEY="sk-..."
export AIFIX_FIXER__MODEL="qwen3-coder-flash"
export AIFIX_DETECTOR__BASE_URL="$AIFIX_FIXER__BASE_URL"
export AIFIX_DETECTOR__API_KEY="$AIFIX_FIXER__API_KEY"
export AIFIX_DETECTOR__MODEL="$AIFIX_FIXER__MODEL"
export AIFIX_PRICE_MAP='{"qwen3-coder-flash": [0.0003, 0.0012]}'

# 3. 指向你项目自己的解释器
export AIFIX_TEST_PYTHON=/path/to/你的项目/.venv/bin/python

# 4. 空跑：不调用任何模型、不花一分钱，只看它认不认得你的项目
cd /path/to/你的项目
aifix run . --dry-run
```

`--dry-run` 要看到的是这样一份报告：

```
- 适配器：pytest
- 修复：**0 / 0**          ← 你的仓库现在是全绿的，正常
- 成本：¥0.0000（0 tokens）
```

**这一步能挡掉绝大部分接入失败**：适配器认不认得、解释器对不对、工作区干不干净、
测试跑不跑得起来，全在这里暴露。

### 再试一次复现

这一步单独量「模型读一段人话能不能写出复现测试」—— 它是 issue 那条路的天花板。

```bash
# 拿你项目历史上一个真实的修复提交，用它的 commit message 当缺陷报告
git log -1 --format='%s%n%n%b' <某个 fix commit> > /tmp/issue.md
git worktree add /tmp/before <某个 fix commit>^   # 回到缺陷还在的那个状态

aifix reproduce /tmp/before --issue-text /tmp/issue.md
```

退出码 0 = 写出了复现测试且它真的红了。多试几个 commit，心里对成功率有个数再往下走。

---

## 第 3 步：把 workflow 放进你的仓库

### 两个必须理解的前提

**一、aifix 和你的项目要用两个不同的 Python 环境。**

aifix 自己依赖 langgraph、pydantic、openai；你的项目有自己的依赖。装在一起的话，
轻则版本冲突装不上，重则**装上了、但你项目的测试环境被污染了** —— 而 baseline 是在
那个环境里跑的，污染的后果是一批凭空多出来的红。

所以：aifix 装在 `/tmp/aifix-venv`，你的项目装在 `.venv`，用 `AIFIX_TEST_PYTHON`
把两者接起来。

**二、这个文件必须在默认分支上。**

GitHub 只从默认分支加载 `issues` / `issue_comment` 的 workflow。在特性分支上改它是调不通
的 —— **而且不报错，只是永远不触发。**

---

### Python / pytest 项目（完整可复制）

`.github/workflows/aifix.yml`：

```yaml
name: aifix

on:
  # 正文第一行是 /aifix 的新 issue —— 主入口，开 issue 即触发。
  # 只认 opened：改自己的 issue 正文是完全静默的，接了 edited 就等于允许
  # 一条半年前的 issue 被悄悄改成 /aifix 开头来触发。
  issues:
    types: [opened]
  # 第一行是 /aifix 的评论 —— 再跑一次、补充说明、回答上一轮的提问。
  issue_comment:
    types: [created]

# 同一个 issue 同时只跑一个。手滑连点两次 = 两倍开销，而且两个 run 会抢同一个 worktree 路径
concurrency:
  group: aifix-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  fix:
    # 便宜的前置过滤：没带命令前缀的在这里就被挡掉，一秒 runner 都不花。
    # 权威判定（含全部权限判断）在 aifix 的 authorize() 里 —— 两处都要，
    # 这里挡不住的由代码挡。
    #
    # **这里刻意不判权限**：没权限的人打了 /aifix 要收到一条说明，而 job 的
    # if: 拦下来的事件根本不会起 job，也就没有东西能发出那条回帖。
    # 代价是任何人开一个 /aifix 开头的 issue 都会起一次约 1~2 分钟的 job
    # （零模型调用，随即以一条权限说明收场）。
    if: >
      (github.event_name == 'issues' &&
       startsWith(github.event.issue.body, '/aifix')) ||
      (github.event_name == 'issue_comment' &&
       startsWith(github.event.comment.body, '/aifix'))

    runs-on: ubuntu-latest
    # 必须显著大于 AIFIX_BUDGET_WALL_SECONDS。Actions 的超时是**杀进程**：
    # aifix 里那个「保证报告先落地」的分支根本执行不到，跑一小时什么都留不下。
    timeout-minutes: 90

    # 显式写全。不写的话仓库默认可能是宽松的，而「我以为它是最小权限」
    # 是这类事故最常见的开头。
    permissions:
      contents: write          # 推交付分支与 aifix/traces
      issues: write            # 状态评论、reaction
      pull-requests: write     # 开 PR

    steps:
      - uses: actions/checkout@v4
        with:
          # 要完整历史：交付分支是从 HEAD 长出来的新分支，
          # 从浅克隆推新分支在部分场景下会被拒（shallow update not allowed）
          fetch-depth: 0
          # 不要设成 false —— aifix 要用这份凭据 push 交付分支

      # ┌───────────────────────────────────────────────────────┐
      # │ 你的项目自己的测试环境。把你 ci.yml 里那段搬过来即可。   │
      # │ 唯一要求：装完之后 .venv/bin/python 能跑起你的测试。     │
      # └───────────────────────────────────────────────────────┘
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: 装项目依赖
        run: |
          python -m venv .venv
          .venv/bin/pip install --upgrade pip
          .venv/bin/pip install -e ".[dev]"      # ← 换成你项目的装法

      # 工作区必须干净，否则 aifix 的 preflight 会拒绝启动。
      # 装依赖有时会改动被跟踪的文件（典型：uv sync 重写 uv.lock）。
      - name: 确认工作区干净
        run: |
          if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
            echo "::warning::安装步骤改动了被跟踪的文件，已还原："
            git status --porcelain --untracked-files=no
            git checkout -- .
          fi

      # ┌───────────────────────────────────────────────────────┐
      # │ aifix 装在一个**独立**的 venv 里，不碰你项目的环境       │
      # └───────────────────────────────────────────────────────┘
      - name: 装 aifix
        run: |
          python -m venv /tmp/aifix-venv
          # 发行名是 aifix-code，装完命令叫 aifix。
          # **CI 上钉住版本**：不钉的话某天 aifix 发个新版，你的流水线行为
          # 会在你没改过任何东西的情况下变掉 —— 而它是会花钱、会开 PR 的。
          /tmp/aifix-venv/bin/pip install --quiet "aifix-code==0.1.0"
          /tmp/aifix-venv/bin/aifix --help > /dev/null && echo "aifix 就绪"

      - name: aifix issue handle
        run: /tmp/aifix-venv/bin/aifix issue handle
        env:
          # gh CLI 与 git push 都用它
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

          # 两条模型路由。variable 名与环境变量同名 —— 设什么就是什么，
          # 换模型不用改这个文件（改它要合进默认分支才生效）
          AIFIX_FIXER__MODEL:    ${{ vars.AIFIX_FIXER__MODEL || 'qwen3-coder-flash' }}
          AIFIX_FIXER__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_FIXER__API_KEY:  ${{ secrets.AIFIX_API_KEY }}
          AIFIX_DETECTOR__MODEL:    ${{ vars.AIFIX_DETECTOR__MODEL || 'qwen3-coder-flash' }}
          AIFIX_DETECTOR__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_DETECTOR__API_KEY:  ${{ secrets.AIFIX_API_KEY }}

          # 价格表用 variable 不用 secret：它不是机密，而 secret 在日志里会被
          # 遮成 ***，你反而看不出它配没配对 —— 而没配价格表 = 成本闸永远不触发
          AIFIX_PRICE_MAP: ${{ vars.AIFIX_PRICE_MAP }}

          # 额外获准触发的登录名，逗号分隔（可选）。默认已放行仓库所有者、
          # 有写入权限的协作者、组织仓库的成员 —— 这份名单是**加法**
          #   gh variable set AIFIX_ALLOWED_USERS --body "alice,bob"
          AIFIX_ALLOWED_USERS: ${{ vars.AIFIX_ALLOWED_USERS }}

          # ★ 关键：跑你项目测试的解释器。别靠自动探测 ——
          #   runner 上 clone 出来没有 .venv，探测落空会退回 aifix 自己的
          #   解释器，然后是一整批 collection error
          AIFIX_TEST_PYTHON: ${{ github.workspace }}/.venv/bin/python

          AIFIX_BUDGET_CNY: ${{ vars.AIFIX_BUDGET_CNY || '15.0' }}
          AIFIX_BUDGET_WALL_SECONDS: "3600"
          # 复现那一步的思考模式默认关。`|| 'false'` 不能省：variable 未设置时
          # Actions 给的是**空串**，而空串的含义是「随端点默认」= 开，正好相反
          AIFIX_REPRODUCER_THINKING: ${{ vars.AIFIX_REPRODUCER_THINKING || 'false' }}

      # 崩了才最需要它。`if: always()` 不是可选的 ——
      # job 失败时不上传，等于恰好在最需要诊断数据的那一次把它扔了
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: aifix-trace-${{ github.event.issue.number }}-${{ github.run_id }}
          path: .aifix/runs/
          retention-days: 30
```

**用 uv 的项目**，把装依赖那步换成：

```yaml
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen        # --frozen：别让它重写 uv.lock 把工作区弄脏
```

`AIFIX_TEST_PYTHON` 仍然是 `${{ github.workspace }}/.venv/bin/python`（`uv sync` 就建在
那儿）。

---

### Java / Maven 项目（完整可复制）

与上面的区别只有两处：不需要项目的 Python 环境（`mvn` 不走 Python 解释器），但
**必须预热 `~/.m2`**。

```yaml
name: aifix

on:
  # 主入口：正文第一行是 /aifix 的新 issue。理由与判据同上面那份
  issues:
    types: [opened]
  # 再跑一次 / 补充说明 / 回答提问
  issue_comment:
    types: [created]

concurrency:
  group: aifix-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  fix:
    # 只过滤命令前缀，**不判权限** —— 理由见上面那份模板里的注释
    if: >
      (github.event_name == 'issues' &&
       startsWith(github.event.issue.body, '/aifix')) ||
      (github.event_name == 'issue_comment' &&
       startsWith(github.event.comment.body, '/aifix'))

    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: write
      issues: write
      pull-requests: write

    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}

      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '21'
          cache: maven          # 命中缓存时下面的预热几乎是白拿

      # aifix 自己是个 Python 程序（要求 3.11+），哪怕你的项目是纯 Java。
      # 它跑测试用的是 `mvn`，不走这个解释器。
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      # ★ 必须有。aifix 的适配器命令是 `mvn -B -q -o clean test`，**带 -o（离线）**，
      #   而 runner 上 ~/.m2 是空的。
      #
      #   注意最后那句 `mvn -B -q clean` 不是收尾，**是预热的一部分**：
      #   `mvn test` 不会下载 maven-clean-plugin，离线仓库里缺它时 `clean` 这一
      #   阶段当场失败，表现是 surefire 一份报告都没有 —— 被 aifix 报成
      #   「测试进程没能正常跑完」。实测栽过一次，查了一轮才定位到。
      #
      #   通则：预热要覆盖**实际会跑的那条命令**，不是「差不多的那条」。
      - name: 预热 ~/.m2
        run: |
          set -eu
          mvn -B -q test || true        # 测试红不红无所谓，构件拉全就行
          test -d target/surefire-reports \
            || { echo "::error::预热失败：surefire 没写出报告，~/.m2 可能没拉全"; exit 1; }
          mvn -B -q clean

      - name: 确认工作区干净
        run: |
          if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
            git status --porcelain --untracked-files=no
            git checkout -- .
          fi

      - name: 装 aifix
        run: |
          python3 -m venv /tmp/aifix-venv
          # 理由同 pytest 那份：CI 上钉住版本
          /tmp/aifix-venv/bin/pip install --quiet "aifix-code==0.1.0"

      - name: aifix issue handle
        run: /tmp/aifix-venv/bin/aifix issue handle
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AIFIX_FIXER__MODEL:    ${{ vars.AIFIX_FIXER__MODEL || 'qwen3-coder-flash' }}
          AIFIX_FIXER__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_FIXER__API_KEY:  ${{ secrets.AIFIX_API_KEY }}
          AIFIX_DETECTOR__MODEL:    ${{ vars.AIFIX_DETECTOR__MODEL || 'qwen3-coder-flash' }}
          AIFIX_DETECTOR__BASE_URL: ${{ secrets.AIFIX_BASE_URL }}
          AIFIX_DETECTOR__API_KEY:  ${{ secrets.AIFIX_API_KEY }}
          AIFIX_PRICE_MAP: ${{ vars.AIFIX_PRICE_MAP }}

          # 额外获准触发的登录名，逗号分隔（可选）。默认已放行仓库所有者、
          # 有写入权限的协作者、组织仓库的成员 —— 这份名单是**加法**
          #   gh variable set AIFIX_ALLOWED_USERS --body "alice,bob"
          AIFIX_ALLOWED_USERS: ${{ vars.AIFIX_ALLOWED_USERS }}
          # Maven 项目**不要**设 AIFIX_TEST_PYTHON —— 它是给 pytest 适配器用的
          AIFIX_BUDGET_CNY: ${{ vars.AIFIX_BUDGET_CNY || '15.0' }}
          AIFIX_BUDGET_WALL_SECONDS: "3600"

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: aifix-trace-${{ github.event.issue.number }}-${{ github.run_id }}
          path: .aifix/runs/
          retention-days: 30
```

> **Maven 侧的两条已知边界**：适配器只映射**标准布局**（`src/main/java` / `src/test/java`），
> 多模块工程（每个 `<module>` 各有自己的 src）不在支持范围内；`source_suffixes` 只认
> `.java`，改 `pom.xml` 才能修好的问题它定位不到。

---

## 第 4 步：配 secrets 和 variables

```bash
# ── 机密（会在日志里被遮成 ***）
gh secret set AIFIX_BASE_URL          # 端点，要以 /v1 结尾
gh secret set AIFIX_API_KEY

# ── 非机密（用 variable，这样日志里看得见）
gh variable set AIFIX_FIXER__MODEL    --body qwen3-coder-flash
gh variable set AIFIX_DETECTOR__MODEL --body qwen3-coder-flash
gh variable set AIFIX_PRICE_MAP       --body '{"qwen3-coder-flash": [0.0003, 0.0012]}'
gh variable set AIFIX_BUDGET_CNY      --body 15.0

# 可选：额外获准触发的人（默认已放行所有者/协作者/组织成员）
gh variable set AIFIX_ALLOWED_USERS   --body "alice,bob"
```

### 三件容易搞错的事

**一、`AIFIX_PRICE_MAP` 用 variable，不要用 secret。** 模型名和价格表都不是机密，而
secret 在日志里会被遮成 `***` —— 跑错模型时你从日志里根本看不出来。而没配价格表的后果
更直接：成本恒算成 0，**成本闸永远不会触发**。

（顺带：显式设了 `AIFIX_BUDGET_CNY` 却没配价格表时，aifix **当场拒绝启动**并告诉你为
什么 —— 与其给一个假的保证，不如现在就停。）

**二、价格表是扁平价表**，格式是 `{模型名: [输入价/千token, 输出价/千token]}`。不是分档
表。传错格式会在启动阶段就被拒绝。

**三、两条路由可以指向两家不同的供应商** —— 诊断挑便宜的、修复挑强的。不设就沿用同一套：

```bash
gh secret set AIFIX_DETECTOR_BASE_URL   # 只在换供应商时才需要
gh secret set AIFIX_DETECTOR_API_KEY
```

（上面那份 workflow 里两条路由都指向同一个 secret；要分开的话把 `AIFIX_DETECTOR__BASE_URL`
那两行改成对应的 secret 即可。）

---

## 第 5 步：仓库设置里那两格

`permissions:` 给够了**也不行**，还有两处仓库级设置：

1. **Settings → Actions → General → Workflow permissions**
   → 勾上 **「Allow GitHub Actions to create and approve pull requests」**

   不勾的话 PR 开不出来，报的是 *not permitted to create and approve pull requests*。
   （aifix 会把这句话连同「去哪一格勾」一起回帖到 issue 上 —— 这是实测撞过的第一名。）

2. **分支保护 / rulesets**：确认没有规则匹配 `aifix/*`。

   aifix 推的是一条**新分支**（`aifix/<run_id>`），不推你的默认分支，所以 main 上的保护
   规则不影响它。但如果你有仓库级的 push restriction 或者匹配所有分支的 ruleset，就会被拒
   —— 表现是「修复跑完了，但分支推不上去」。

---

## 第 6 步：第一次真跑与验收清单

### 怎么触发

**主入口：开一个 issue，正文第一行写 `/aifix`，其余部分写缺陷描述。**

```
/aifix
购物车为空时调 total() 抛 KeyError，期望返回 0。

复现：
    Cart().total()
```

那行 `/aifix` 是给机器看的标记，**会被去掉再交给模型** —— 模型读到的就是你写的描述。

**另一条入口：在已有的 issue 下评论。** 用来再跑一次，或者回答 aifix 提的问题：

```
/aifix        # 对这个 issue 再跑一次
/aifix 2      # 回答它刚才问的问题，选第 2 项
```

评论触发时模型读的是 **issue 正文**（不是你这条评论），所以那条路会同时检查评论者
**和** issue 作者的权限。

两条入口都**只看第一行** —— 正文里引用别人的话、或贴一段命令示例，不会误触发。

### 它会依次做这些事

```
👀 给触发的那条评论加 eyes reaction        ← 评论触发时才有。Actions 排队有几十秒
                                              空窗，这个 reaction 是「命令被听见了」
                                              的回执；issue 正文触发时跳过这一步
   ↓
   模型读 issue → 写一条复现测试
   ↓
   跑一遍确认它真的红了（红检）
   ↓
   提交这条测试 → 跑核心循环去修
   ↓
   推分支 → 开 PR → 更新状态评论（一条，不刷屏）
```

### 三种结局，都是正常的

| 你会看到 | 意思 |
|---|---|
| 一条回帖说**缺什么信息** | issue 写得不够具体，模型如实说了写不出复现。补充 issue 再来一次 |
| 一条回帖 + 一个 **`aifix/<run_id>` 分支**（不开 PR） | 没修好，但**那条红着的复现测试本身就是产出** —— 回帖里有 `git fetch` 命令，直接接手 |
| 一个 **`fix: ...`** 的 PR | 修好了，报告在 PR 正文里 |

**Actions 页面绿着**是这三种的共同结果 —— 写不出复现、没修好都是正常结论，不是错误。
只有真崩了（环境不对、端点不通、推不上去）才会红。

### 第一次跑完，照着这张表验一遍

```bash
gh pr checkout <PR 号>

# 1. 测试文件一个字节都不许变（除了新增的那条复现测试）
git diff main...HEAD -- tests/

# 2. 分支上真的有提交，不是空 PR
git log --oneline main..HEAD

# 3. 亲眼看一遍 diff —— 这是唯一那道人闸
git diff main...HEAD

# 4. 在你自己机器上跑一遍全量，别只信报告
pytest        # 或 mvn test
```

**第 4 步别省。** aifix 的判定是可信的，但它是在 runner 那个环境上做出的 —— 而
`baseline` 里如果本来就有别的红（runner 镜像漂移），「这个补丁没弄坏别的」这个结论要打
折扣。这种情况 PR 正文里会有一段明确的告警，写着 baseline 里另外那几个红是谁。

### 第一次就该去看的地方

不管成没成，`.aifix/runs/` 会作为 artifact 上传（30 天）。下载解压后：

```bash
aifix replay <run_id> --repo <解压出来的目录>
```

能看到模型每一步读了什么、改了什么、为什么被守卫拦下。详见
[diagnostics.md](diagnostics.md)。

---

## 授权模型：谁能触发，怎么给别人开权限

一条原则贯穿全部判据：

> **触发权 = 已经能改这个仓库的人。**

因为 issue 正文会驱动模型改代码、开 PR。一个本来就能直接推代码的人驱动它，不增加任何新
风险；反过来，一个没有写入权限的人能驱动它，等于给了他一条**间接的写路径** —— PR 的内容
由他的文字决定。

### 默认放行谁

四条判据，任一成立即可（零网络调用，只看事件载荷里已有的字段）：

| 判据 | 覆盖谁 |
|---|---|
| 登录名 == 仓库账号本身 | 个人仓库的主人 |
| `author_association` 是 `OWNER` | 同上，走 GitHub 的关系分类 |
| `author_association` 是 `COLLABORATOR` | 被加为协作者的人 —— **按定义有写入权限** |
| `author_association` 是 `MEMBER` **且仓库属于组织** | 组织成员 |

**明确不放行 `CONTRIBUTOR`。** 它的含义只是「有 commit 进过这个仓库」—— 一年前合过一个改
错别字的 PR 就永久是 CONTRIBUTOR，而他今天对这个仓库没有任何权限。

### 给别人开权限：两条路

**路一：加成协作者（推荐）**

```
Settings → Collaborators → Add people
```

用 GitHub 自己的权限体系，不另造一套 —— 人离职、权限调整时，aifix 的行为跟着一起变，不会
漂移。

**路二：显式白名单**

```bash
gh variable set AIFIX_ALLOWED_USERS --body "alice,bob"
```

它是**加法不是替换**：上面那四条照旧生效，这份名单只把按 `author_association` 认不出来的
人点名放进来。大小写不敏感、整名匹配（`alice` 不会放行 `alicexyz`）。

用 variable 不用 secret —— 它不是机密，而 secret 在日志里会被遮成 `***`，出问题时你看不出
到底谁被放行了。

代价要认：**它和仓库的真实权限会漂移。** 人离职了、协作者移除了，名单还留着。所以它适合
「某个具体的人，暂时」，长期授权应该走路一。

### 组织仓库

**现在开箱可用**（早先的版本只认 `OWNER`，组织仓库整个跑不通）。组织成员拿到的
`MEMBER` / `COLLABORATOR` 都被认。

一条已知的宽松处：**`MEMBER` 不保证对这一个仓库有写入权限** —— 它只说明「是这个组织的
成员」。要精确到写入权限得调一次 GitHub API，那会让这道判定从纯函数变成有网络 IO 的东西，
而它是全项目最要紧的一道判定，保持纯函数才能被脱网穷举。

需要更严的话，两条路：把该给权限的人加成 COLLABORATOR，或者反过来用白名单点名。

### 外人报的 bug 怎么办

外人开的 `/aifix` issue 会被拒，并收到一条说明。这是有意的 —— 他的正文会作为输入喂给模型。

要修的话，**由有权限的人自己新开一个 issue、用自己的话复述一遍**。回帖里就是这么说的。

（不能用「有权限的人在他的 issue 下评论 `/aifix`」绕过：那条路会同时检查评论者**和** issue
作者，正是为了堵住「外人提一个藏了指令的 issue，等有权限的人顺手打上 `/aifix`」这条攻击
路径。）

### 改了授权逻辑一定要跑这个

```bash
uv run pytest -q tests/test_issue_event.py tests/test_workflow.py
```

前者把每一条授权判据都钉住了，后者钉住 workflow 那层过滤。它们红了说明你放开的东西比你
以为的多。

---

## 成本：这一下大概花多少钱

一次 `/aifix` 触发包含两段模型调用：**写复现测试**（一轮）+ **修复**（最多
`max_attempts` 轮，每轮一次诊断 + 一次修复）。

参考读数（39 个真实任务，`qwen3-coder-flash`，只算修复那一段）：

| | 每任务均值 |
|---|---|
| tokens | 238,070 |
| 成本 | $0.1241（≈ ¥0.89） |

> 这批读数是换币种之前测的，**原始单位是美元**；括号里的人民币按默认汇率 7.2 折算，
> 不是重新测出来的数。

复现那一步另算，取决于你仓库的规模 —— 实测在一个中等仓库上用较强的模型跑要 $0.21
（≈ ¥1.5）以上。

### 三个闸，配置里都有

```bash
AIFIX_BUDGET_CNY=15.0           # 人民币（需要价格表）
AIFIX_BUDGET_TOKENS=500000      # token
AIFIX_BUDGET_WALL_SECONDS=3600  # 墙钟
```

**准确的语义是「越线之后不再发起新的模型调用」，不是「绝不超过这个数」** —— 成本只有在
调用返回后才知道，所以越线时那一次调用必然已经花掉。超支上界是可陈述的：**一次模型调用**。

### 两条实用建议

**别把预算设太紧。** 实测（当时的币种是美元）把每任务上限从 0.60 调到 0.20 时，某个任务 1 轮就被掐断判成
「没修好」；放回去之后同一个任务修好了。**预算设太紧会把「模型不行」和「额度不够」混成
同一个数字。**

**先用便宜的模型验链路。** 第一次跑要验的是管道通不通，不是修复质量 —— 拿贵的模型验管道，
贵在了不产生信息的地方。

---

## 接入时最容易踩的七个坑

按撞上的概率排序。

### 1. workflow 不在默认分支上 → 永远不触发，且不报错

`issue_comment` 的 workflow **只从默认分支加载**。这也是为什么建议[先在本地跑通](#第-2-步本地先跑一次强烈建议)。

### 2. 没配 `AIFIX_TEST_PYTHON` → 一整批 collection error

runner 上 clone 出来没有 `.venv`，自动探测落空会退回 aifix 自己的解释器 —— 而你项目的
测试依赖不在那里面。

表现是 aifix 中止并说：

> 本次 baseline 的 N 个失败里有 M 个是**整个测试文件没能跑起来**…… 这不是模型的问题

这道闸是有意的：把那些 id 当成待修用例排进队列，模型会被派去修「这台机器上缺了点什么」
这件事，**会真花钱，而报告最后写的是「模型没修好」—— 一个成绩，其实是一次故障。**

### 3. 装依赖弄脏了工作区 → preflight 拒绝启动

最常见的是 `uv sync` 重写 `uv.lock`。上面那份 workflow 里的「确认工作区干净」那一步就是
处理它的；用 uv 的话更干净的做法是 `uv sync --frozen`。

**为什么这道闸不能放松**：baseline 是从 HEAD 算出来的，工作区另有改动的话，算出来的失败
集合和你眼前看到的对不上。（未跟踪文件不算 —— `__pycache__` 这些根本进不去 worktree。）

### 4. 没有写入权限的人触发 → 被拒并收到一条说明

这是设计使然。给他开权限的两条路（加成协作者、或 `AIFIX_ALLOWED_USERS`）见[授权模型](#授权模型谁能触发怎么给别人开权限)。

外人报的 bug：由有权限的人**自己新开一个 issue 复述一遍**，别用「在他的 issue 下评论」绕过 —— 那条路会同时检查评论者和 issue 作者。

### 5. Maven 项目没预热 `~/.m2` → 「测试进程没能正常跑完」

适配器命令带 `-o`（离线）。而且预热必须**覆盖实际会跑的那条命令** —— `mvn test` 不会下载
`maven-clean-plugin`，而适配器跑的是 `mvn -o clean test`。

### 6. 端点有 IP 白名单 → 整条路线不成立

这就是[第 1 步](#第-1-步连通性自检)要先做的原因。

### 7. job 超时小于等于墙钟预算 → 跑了一小时什么都没留下

Actions 的超时是**杀进程**。aifix 里那个「保证报告先落地」的分支根本执行不到。
**让 aifix 自己的墙钟闸先响，硬杀只是兜底。**

推荐比例：`timeout-minutes: 90` 配 `AIFIX_BUDGET_WALL_SECONDS: "3600"`（60 分钟）。

---

## 不接 GitHub，只在本地用

不想接 Actions 也完全可以用 —— 主命令 `aifix run` 本来就是独立的。

```bash
# 你的仓库现在有几个红的用例，让它去修
cd /path/to/你的项目
aifix run . --budget 1.0

# 只修其中一个
aifix run . --test 'tests/test_cart.py::test_total'

# 模型停下来问你问题时
aifix answer 1

# 跑完看一眼再合
git diff main aifix/<run_id>
git merge aifix/<run_id>
```

它**不 push、不 merge、不碰你的主工作区、不删任何分支** —— 交付物是一条本地分支，合不合
完全是你的事。完整清单见 [safety.md 的不可逆动作清单](safety.md#不可逆动作清单)。

想要一条 cron 定时跑的话，用 `--quiet` 加个 `schedule` 触发的 workflow 即可，但注意
**它仍然只会开分支，不会自动合**。

---

## 接下来

| 想干什么 | 看哪儿 |
|---|---|
| 调旋钮（预算、轮数、超时、守卫） | [configuration.md](configuration.md) |
| 弄明白它凭什么敢改我的代码 | [safety.md](safety.md) |
| 跑挂了要查 | [diagnostics.md](diagnostics.md) |
| 在**我自己的仓库**上量一下它到底行不行 | [evaluation.md](evaluation.md) —— `aifix mine` 能从你的 git history 里挖出自带标准答案的任务集 |
| 我的项目既不是 pytest 也不是 Maven | [adapters.md](adapters.md#怎么加第三个适配器) |
| issue 那条流水线内部是怎么走的 | [issue-driven.md](issue-driven.md) |
