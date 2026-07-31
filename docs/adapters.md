# 适配器：怎么支持一门新语言，以及抽象是怎么被验出来的

这份文档回答两个问题：**`ProjectAdapter` 到底要求你回答什么**，以及**为什么我们知道这个抽象是对的（而不是恰好长得像 pytest）**。

流程见[架构](architecture.md)。

---

## `ProjectAdapter` 协议

`src/aifix/adapters/base.py`。一个 `name` 属性 + **11 个方法**。核心循环通过它把「某种语言的测试工程」翻译成几个自己认识的问题。

数字 11 不是随口说的：`tests/test_nodes_preflight_baseline.py` 从协议对象上反射出成员列表，断言 `len(_PROTOCOL_MEMBERS) == 11`，再对注册表里的每个实现**逐成员比签名**。那条 `== 11` 是防空转的 —— 协议一旦改成别的写法（比如成员只剩注解），成员列表会变成空，循环一次不跑而测试照样绿。

### 探测

| 成员 | `PytestAdapter` | `MavenAdapter` |
|---|---|---|
| `detect(repo) -> bool`（静态） | `pytest.ini` / `tox.ini` / `setup.cfg` / `pyproject.toml` / `conftest.py` 任一存在，或有 `tests/` 目录 | `pom.xml` 在根目录 |

### 跑测试

| 成员 | `PytestAdapter` | `MavenAdapter` |
|---|---|---|
| `full_test_command() -> list[str]` | `<测试解释器> -B -m pytest -q -p no:cacheprovider -o junit_family=xunit1 --junitxml=.aifix-report.xml` | `mvn -B -q -o clean test -Dmaven.test.failure.ignore=true` |
| `scoped_test_command(test_ids) -> list[str]` | 同上，换 `--junitxml=.aifix-recheck.xml`，末尾追加 id | 追加 `-Dtest=<逗号连接>` 与 `-DfailIfNoSpecifiedTests=false` |
| `report_paths(worktree, scoped=False) -> list[Path]` | 单份：`.aifix-report.xml` 或 `.aifix-recheck.xml` | 整目录：`target/surefire-reports/TEST-*.xml` |

**命令不接收报告路径** —— 报告写到哪里是构建体系自己的事，Maven surefire 只认 `target/surefire-reports/`，调用方指定不了。

几个非显然的选项，每一个都是踩出来的：

- pytest 的 `-o junit_family=xunit1`：xunit2（pytest 的默认）**不写** `<testcase file=...>`，而 `file` 是把 junit 报告里的用例还原成可重跑 node id 的唯一可靠依据
- pytest 的 `-B` / `-p no:cacheprovider`：不写 `__pycache__` 与 `.pytest_cache`。理由**不是**「会被扫进交付分支」（`Worktree.commit` 只 add 记账过的路径），而是未跟踪产物**跨状态存活** —— 同一个 worktree 会被 `git checkout --force` 在 `C^` 和 `C` 之间来回切，而 checkout 不碰未跟踪文件
- Maven 的 `clean`：`mvn test` **不清空** `target/surefire-reports/`，而 `report_paths` 只看文件系统当前状态。跑一次全量留下 A、B、C，再跑只测 A 的复跑，目录里仍躺着上一轮的 B、C —— flaky 确认据此判定，**不报错，只是判错**
- Maven 的 `-Dmaven.test.failure.ignore=true`：测试一红 `mvn` 就以非 0 退出，而报告那时**已经写出来了**。不加的话调用方会把退出码读成「没跑成」，而「没跑成」和「跑完了、有红的」在这个项目里是两种完全不同的结论
- `scoped` 参数是决定性的，不是排版细节：pytest 侧两份报告必须**不同名**（复跑会覆盖掉还要继续用的全量报告）；Maven 侧由 surefire 写在同一个目录，忽略这个参数即可

### 用哪个解释器跑 pytest

`PytestAdapter` 的 argv[0] 曾经写死 `sys.executable`。那等于要求**目标项目的测试依赖装在 aifix 自己的解释器里** —— 真实项目从不满足这一条。实测：拿 aifix 的 venv 去跑 `ai-harness-framework` 的测试是 **11 个 collection error**（`Interrupted: 11 errors during collection`，一个用例都没跑到），换它自己的 `.venv` 是 **673 passed / 3 skipped**。

那 11 个 collection error **不是空气**：pytest 收集中断时照样写出一份完整的 JUnit 报告，里面是一条条文件级 `<error>`，`make_test_id` 会老老实实把它们翻译成 11 个可重跑的 node id 排进队列。所以用错解释器的后果不止是「一个用例都没跑到」，还是「11 个凭空捏造的工单」。`baseline` 之后有一道闸专门查这件事并当场中止，判据见[安全边界](safety.md#baseline-全是收集错误collect-中止)。

现在按三级取值，先到先得：

| 来源 | 取值 | 说明 |
|---|---|---|
| 1. 显式配置 | `AIFIX_TEST_PYTHON` / `AifixConfig.test_python` | 用户说了算，优先级最高。指到一个不可执行的路径时 **preflight 当场中止** |
| 2. 自动探测 | 源仓库的 `.venv/bin/python`，其次 `venv/bin/python`（Windows 是 `Scripts/python.exe`，未在真机验证） | 要求文件存在**且可执行** |
| 3. 回退 | `sys.executable` | 改造之前的唯一行为，逐点不变 |

**探测目标是源仓库，不是 worktree** —— 这不是风格问题：worktree 建在 `.aifix/runs/<id>/tree`（git worktree），评测建在 `git clone --local` 出来的临时目录，两者都**不含** `.venv`（它没被 git 跟踪）。照着 worktree 探，探测永远为空，整个功能静默退化成 `sys.executable` 而不报任何错。解释器路径是绝对的，跑测试的 cwd 是 worktree，跨目录用没有问题。

注入点是**构造参数**（`PytestAdapter(python=...)`），不是新的协议方法：`full_test_command()` 不接参数是协议里写死的，而 `MavenAdapter` 根本不需要解释器（`mvn` 是外部命令）。代价是 `MavenAdapter.__init__` 也得收下这个参数并丢掉 —— 不收的话，`adapter_for` 那行 `ADAPTERS[name](python=...)` 会让任何 Maven 工程在**取适配器**时 `TypeError`。核心循环的四个节点（baseline / detect / fix / verify）统一走 `nodes.baseline.adapter_from_state`，只有它同时握着 `state["repo"]` 和 `state["config"]`；漏掉 fix 那一处的代价最隐蔽：`RunTestsTool` 会用另一个解释器复跑，模型看到的证据和 verify 的判定依据不是同一套环境，而两边都不报错。

#### 换来的真实风险：可编辑安装会让验证悄悄失效

目标项目如果把自己**可编辑安装**（`pip install -e .`）进了那个 venv，site-packages 里会留一条指向**源仓库**的路径记录。于是 `import <目标包>` 可能解析到源仓库那份**没打补丁**的代码，而不是 worktree 里打了补丁的那份 —— 测试照跑、照绿，验证却完全失去意义。这正是这个项目最怕的那类失效：不崩溃、不报错，只有结论是假的。

`ai-harness-framework` 之所以没踩到，不是运气：它的 `.venv` 里**确实**躺着 `_editable_impl_ai_harness_framework.pth`，内容就是源仓库的 `src` 绝对路径；救它的是 `pyproject.toml` 里的 `pythonpath = ["src"]` —— pytest 会把 **worktree 的** `src` 插到 `sys.path` 最前，盖过那条记录。**这不普遍成立**：src 布局 + 没配 `pythonpath` 的项目就会中招。

aifix **不解决**这件事（要么接管目标项目的安装方式，要么改写它的 `sys.path`，两件都超出适配器的职权），只在它发生时出声：`imports_outside_worktree()` 在 baseline 之前跑一次，拿目标解释器问「worktree 里这些顶层包会从哪个文件导入」，凡是解析到 worktree 之外的都往 stderr 打一条警告，并在 trace 里记一条 `imports_outside_worktree` 事实。

这道探测的边界必须写清楚：

- **它是近似**。它用 `python -c` 复现 pytest 的 `sys.path` 前几项（cwd + ini 里的 `pythonpath` + 环境里的 `PYTHONPATH`），复现不了 `conftest.py` 里手写的 `sys.path` 改动、`--import-mode=importlib` 的细节、rootdir 之外的插件。**返回空不等于安全。**
- **它只报警，不拦截**。一个可能误报的信号如果有权中止整个 run，用户为了跑起来就会去关掉它，那比没有更糟。
- **必须读 ini 里的 `pythonpath`**，否则「src 布局 + 配了 `pythonpath`」这个**正常且安全**的配置会被每次误报一次 —— 一条在最常见的健康配置上就会响的警告等于没有。
- **写 stderr 而不是等报告**：报告在整个 run 结束后才渲染，而这句话要挡住的正是「跑了半小时、花了钱、结论是假的」。

自保的办法只有一个，写在 `aifix run --help` 里：在目标项目的 pytest 配置里设 `pythonpath`。

### 分类路径

| 成员 | `PytestAdapter` | `MavenAdapter` |
|---|---|---|
| `test_dirs() -> list[str]` | `["tests", "test"]` | `["src/test"]` |
| `source_suffixes() -> tuple[str, ...]` | `(".py",)` | `(".java",)` |
| `test_selectors(test_files) -> list[str]` | 路径就是选择器，滤掉非 `.py` | `src/test/java/demo/CalcTest.java` → `demo.CalcTest` |

`source_suffixes` 只收**产品代码**的后缀，不收资源/配置：它是 `locate_hit` 的判定依据，衡量的是 Detector 定位**源文件**的能力，掺进数据文件会稀释它。pytest 侧不含 `.pyi`（存根里没有可执行实现，改它修不好任何测试）；Maven 侧不含 `pom.xml`（它确实能让测试转红转绿，但不是 `locate_source` 能指向的东西，塞进 `gold_files` 等于给 Detector 记一个它按设计就拿不到的分）。

`test_selectors` 是「本次 commit 改动过的测试文件路径」→「`scoped_test_command` 认得的选择器」。**这不是恒等映射，只是在 pytest 上恰好长得像恒等映射。** 翻不出来的路径（夹具、测试资源、非标准布局）一律丢掉，不猜 —— 猜错的选择器在两种适配器上都是**静默**的：pytest 收集整轮中止（exit 4）写出一份 `tests="0"` 的空报告，surefire 干脆安静地一个用例都不跑。

### 用例 id

| 成员 | `PytestAdapter` | `MavenAdapter` |
|---|---|---|
| `make_test_id(classname, name, file) -> str` | `tests/test_x.py::TestC::test_y`（三种形状，见下） | `demo.CalcTest#addWorks`；`name` 为空时是**裸类名** |
| `is_file_level_id(test_id) -> bool` | `"::" not in test_id` | `"#" not in test_id` |
| `cases_under(file_id, test_ids) -> set[str]` | 前缀 `文件::` | 前缀 `类#` |

`make_test_id` 把 junit 报告里的一条 `<testcase>` 还原成**可以直接喂回 `scoped_test_command`** 的 id。pytest 侧三种形状全部来自实测：收集错误（`classname` 空，id 就是文件路径）、类内测试（`classname` 尾部超出模块路径的段是类名链，支持嵌套类）、模块级测试。

`is_file_level_id` 问的是「这个 id 指的是一整个测试文件 / 测试类，而不是单个用例吗」。**收集阶段整体失败时报告里发的就是这种 id**：pytest 的测试文件导入失败发一条文件级 `<error>`，surefire 的测试类初始化失败发一条 `name` 为空的 `<testcase>`。挖任务时「测试文件在 `C^` 起不来、在 `C` 正常」是一整类候选 —— 实测本仓库 65 个候选 commit 里 32 个是那个形状（2026-07-28 测；这个比例随仓库历史增长而变，重要的是它是**一整类**而不是零星几个）。

`cases_under` 两侧都比**带分隔符的前缀**而不是裸 `startswith`：`tests/test_xyz.py::t` 不属于 `tests/test_x.py`，`demo.CalcTestHelper#x` 不属于 `demo.CalcTest`。

### 定位

| 成员 | `PytestAdapter` | `MavenAdapter` |
|---|---|---|
| `locate_source(failure, repo) -> list[SourceCandidate]` | 从 traceback 抽 repo 内部帧，**reverse**（Python 由浅入深打印，最深的最可疑） | 从 Java 堆栈抽 `src/main/java` 下的帧，**不 reverse**（Java 由深入浅，栈顶就是抛出点） |

两侧都要求候选**真实存在**：`SourceCandidate.path` 会原样进模型的提示词，一个不存在的路径会让模型去读空文件甚至凭空造改动。

Maven 侧先按包名前缀丢掉框架帧（`org.junit.`、`org.opentest4j.`、`java.`、`jdk.`、`sun.`）—— 断言失败时栈顶清一色是这些包，不筛的话「最可疑的位置」会指向 JUnit 自己。被丢掉的还有测试类自己的帧（它在 `src/test/java`）：**Java 断言失败的堆栈里往往一条产品代码的帧都没有**（被测方法正常返回了，抛异常的是 `assertEquals`），这时候候选为空是诚实的答案，不是缺陷。

---

## 写一个新适配器要做什么

1. **实现 11 个成员**，签名逐字对上 `ProjectAdapter`（测试会比）。
2. **登记进 `src/aifix/nodes/baseline.py` 的 `ADAPTERS`**。这是**全项目唯一的注册表**，`preflight_node` 与 `aifix mine` 都走 `detect_adapter` 这唯一一份探测。
3. **注意插入顺序 —— dict 的插入顺序就是探测顺序，改动顺序等于改变探测语义。** 通则：`detect` 越具体的排越前，兜底式的排最后。现在 Maven 在 pytest 前面，因为 `MavenAdapter.detect` 要求根目录有 `pom.xml`（具体、几乎不会误判），而 `PytestAdapter.detect` 极宽松 —— `pyproject.toml` 或 `tests/` 存在就认领，而 Java 工程的工具链里带 Python 脚本是常事。**反过来排的后果不是报错而是静默**：Maven 工程被判成 pytest 工程，baseline 跑 pytest 命令收不到任何用例，报告写「0 个失败」。
4. **让测试命令自己清报告**，或保证每次跑之前目录是干净的（见上面 Maven 的 `clean`）。
5. **写一条真跑构建工具的端到端验收。** 「产出了 N 条记录」证明不了任何事 —— 见下面「验收」。

新增协议成员时，`_PROTOCOL_MEMBERS` 的那个数字要跟着改（它会当场红，这是有意的）。

---

## 压轴：一个只有单一实现的接口，无法区分「抽象对」和「抽象恰好长得像那一个实现」

`MavenAdapter` 是第二个实现。它撞出了**六处裂缝**，全部已修，全部有真跑 `mvn` 的验收。

| # | 位置 | 症状 |
|---|---|---|
| 1 | `report_glob() -> str` | 报告位置是**一组**路径不是一个（surefire 每个测试类写一份 `TEST-*.xml`） |
| 2 | `preflight` 里的**第二份**适配器注册表 | `MavenAdapter` 登记好了却永远探测不到，加了等于没加，且两处都不报错 |
| 3 | `split_paths` 的源文件后缀写死 `.py` | Java 源码全部落空 → `gold_files` 恒空 → `is_candidate` 恒 `False` |
| 4 | `verify_commit` 的 scoped 范围写死 `.py` **路径** | Maven 要的是 `-Dtest=` 加类名，不认路径 |
| 5 | `"::" not in id` 判文件级 id | `::` 是 pytest 语法，**每个** Maven 候选被静默丢掉 |
| 6 | `_cmd_mine` 写死 `PytestAdapter()` | 前五处全修好也还是 0 个任务 |

**六处里有五处的症状是同一种：静默产出 0 个任务，不报任何错。**

这不是巧合 —— 挖掘链路上每一步的失败模式都是「筛掉」，而筛空与「这个仓库最近没有红转绿的提交」这个**正常结果**长得一模一样。写新适配器时，这条链路上的每一处判据都要问一遍：**它是不是在用 pytest 的语法回答一个通用问题。**

### 逐处

**裂缝 1**：`run_full_suite` / `run_scoped` 都写死 `parse_junit([worktree / report], ...)`，一个路径。修法是把接口改成 `report_paths(worktree, scoped=False) -> list[Path]`，`report_glob()` **整个删掉**，不保留任何过渡形态 —— 留着它等于让协议同时有两个回答报告位置的成员，而 Maven 只能回答其中一个。规格对这一处的评价值得抄下来：*「它证明了抽象不对，也证明了这个验证有价值 —— 如果 `MavenAdapter` 顺顺当当地接上了，那才说明这个里程碑白做了。」*

**裂缝 2**：`preflight` 另存了一份 `ADAPTERS = [PytestAdapter]`，而 `adapter_name` 由**它**决定。现在探测只有 `baseline.detect_adapter` 一份。

**裂缝 3**：`split_paths` 的 `source_suffixes` 现在**没有默认值**，这是有意的 —— 默认 `(".py",)` 正是这个函数此前的 bug，少传一个参数当场 `TypeError`，比静默退化成「只认 Python」好。

**裂缝 4**：`scope = [p for p in test_files if PurePosixPath(p).suffix == ".py"]` 让 Maven 任务的 `test_files` 全被滤空 → 在 `materialize` **之前**就 `return []` → `on_progress` 看到 `n=0`。实测：对一个真有红转绿提交的 Maven 仓库产出 0 个任务，`mvn` 一次都没起，全程 **0.94 秒**。

不能简单换成 `source_suffixes()` 或添一个 `.java`：`scope` 原样进 `scoped_test_command`，而 surefire 的 `-Dtest=` 只认全限定类名 —— 喂路径进去不报错，**安静地一个用例都不跑**。所以新增了协议成员 `test_selectors`。

**裂缝 5**：`MavenAdapter.make_test_id` 产出 `demo.CalcTest#addWorks`，**一个 `::` 都没有**。于是每一个 Maven id 都被判成文件级 id，`_file_went_green` 拿 `startswith("demo.CalcTest#addWorks::")` 去匹配，永远匹配不到，恒返回 `False`，复跑那一步把整批候选清空。

**这一处比裂缝 4 更隐蔽**：修好裂缝 4 之后 `mvn` 会真的跑起来、跑满四个阶段，屏幕上一切正常，结果**仍然是 0 个任务**。

**裂缝 6**：`_cmd_mine` 直接 `PytestAdapter()`，于是**适配层里为 Maven 补的每一处缺口都在这一行之后，全都到不了**。它是裂缝 2 的同一个形状换了个入口。

### 修裂缝 5 时顺手挖出来的一个死分支

按处方直接实现，`MavenAdapter.is_file_level_id` 会是**空转的** —— `make_test_id` 恒定产出 `classname#name`，没有任何 id 会不带 `#`，那个分支永远走不到。Maven 侧「测试类在 `C^` 起不来」这一整类候选照样静默丢掉，只是**换了个丢法**。

所以去问 surefire 它到底怎么写这种报告。实测（`@BeforeAll` 抛异常，surefire 3.2.5 / JUnit 5.10.2）：

```xml
<testsuite name="demo.BootTest" tests="1" errors="1" failures="0">
  <testcase name="" classname="demo.BootTest" time="0.004">
    <error type="java.lang.IllegalStateException" message="类初始化就炸了"/>
```

整个类只发**一条** `name` 为空的 `<testcase>`，两个 `@Test` 方法一条都不发 —— 正是 pytest 侧「文件导入失败发一条文件级 `<error>`」的对应物。而 `make_test_id` 只处理了 `classname` 为空、没处理 `name` 为空，拼出 `demo.BootTest#`。

再实测这个 id 当选择器会怎样：

```
===== -Dtest=demo.BootTest# =====     ← 跑了 3 个用例，两个类的报告都写了
Tests run: 3, Failures: 1, Errors: 1
===== -Dtest=demo.BootTest =====      ← 只跑那一个类
Tests run: 1, Failures: 0, Errors: 1
```

**`类名#` 被 surefire 读成「没有过滤条件」，把整个套件跑一遍。** 挖任务时阶段 3 的复跑会把无关类的失败读成候选自己的失败 —— 不报错，只是判错。改成裸类名（合法选择器，跑整个类），`is_file_level_id` 也随之从空转变成真的有东西可判。

这一条不是推出来的，是去问真实工具才浮出来的。

### 查法

裂缝 6 是靠「顺着 `mine_tasks` 的 `adapter` 参数是谁递进来的」查到的。**`grep -rn '"::"' src/` 找不到它** —— `_cmd_mine` 里那一行既不含 `::` 也不含 `.py`。查法应该是「**谁在构造适配器实例**」而不是「谁写了 pytest 的语法」。

### 已登记、不修的两处

两者都是**算子层自己的限制**，不是适配层的遗漏，代码里有注释说明：

- `eval/mutate.py`：人造变异靠 Python 的 `ast` 定位，`_test_index` 按 `::` 分组、`git ls-files -- '*.py'`、`split_paths(..., (".py",))` 全部写死。换成 `adapter.source_suffixes()` 只会把 `.java` 喂进 `ast.parse`。**变异任务今天只对 Python 工程成立**，`_cmd_mutate` 里写死 `PytestAdapter()` 在这个前提下是对的（与裂缝 6 不同）
- `signals.py`：`public_symbols` / `module_state` 是 Python AST 分析，非 `.py` 跳过。**Java 补丁拿不到「删了公开符号」「新增模块级可变状态」这两个信号**，但照样进 `files_outside_suspect`

### 验收

**「产出了 N 条记录」证明不了任何事。**

`tests/test_maven_mining.py` 造一个两提交的真 Maven git 仓库跑完整的 `mine_tasks`（四个阶段，五次真 `mvn`），断言产出的任务数 > 0、`target_test` 形如 `demo.CalcTest#addWorks`、`gold_files` 是 `src/main/java/` 下的 `.java`，最后把任务 `prepare_task_repo` 到临时目录**真跑一次全量**，确认：

- `demo.CalcTest#addWorks` 在 `fs.ran` 里（真跑到了）**且**在 `fs.ids` 里（真红了）
- `demo.CalcTest#zeroIsStable` 在 `fs.ran` 里**且不在** `fs.ids` 里 —— 不是「整个套件都红」，那种仓库拿任何 target 都能过上半条断言
- 还原出来的树确实是「`C^` 的源码 + `C` 的测试」这个人造状态（逐字比对两个文件）

几条区分度设计同样值得抄：

- **pytest 侧逐点不变要有回归钉住。** 只测 Maven 的话，一个顺手把 pytest 也改坏的实现（`test_selectors` 返回类名、放行夹具）照样能过 Maven 那几条
- **每条 Maven 断言都配反向断言。** 比如「一个无脑返回 `maven` 的 `detect_adapter` 实现必须过不了」
- **防空转计数。** 断言 `calls == {"scoped": 3, "full": 1}` —— 四个阶段必须都真的走到，否则「返回了那一条」可能是提前返回撞上的
- **`-Dtest=demo.CalcTest` 的验收断言 `OtherTest` 一个用例都没跑到。** 只看 `CalcTest` 的话，「正确地只跑了那个类」与「跑了全套」两种结果长得一模一样

---

## 离线跑 Maven：预热必须覆盖 `clean`

适配器的命令是 `mvn -B -q -o clean test`，`-o` 是离线。所以本机 `~/.m2` 里必须先有全部构件 —— 这一点在 CI 上尤其容易栽：GitHub 的 ubuntu 镜像**自带 Maven 但 `~/.m2` 是空的**。

而预热时只跑 `mvn test` **不够**：它不会下载 `maven-clean-plugin`，于是 `mvn -o clean test` 在 `clean` 阶段就失败，surefire 一份报告都写不出来，aifix 报的是「测试进程没能正常跑完」——一句指向目标项目的话，真因在预热那一步漏了一个插件。

实测（2026-07-31）第一次 CI 验收就栽在这里。正确的预热是把实际会跑的那条命令跑一遍：

```bash
mvn -B -q test || true      # 测试预期失败，只要构建跑起来
mvn -B -q clean             # ← 这一句才是关键：它把 maven-clean-plugin 拉下来
```

**通则：预热要覆盖实际会跑的那条命令，不是「差不多的那条」。**

同一条判据也写进了测试的跳过条件（见 `tests/conftest.py` 的 `maven_offline_reason`）——它真起一个最小工程跑一次 `mvn -o test`，而不是查 `mvn` 这个二进制在不在。查错对象的后果是：CI 上不跳过、真跑、真失败，19 个假红进 baseline。

## 相关文档

- [架构](architecture.md) —— 适配器在哪些节点被调用
- [评测](evaluation.md) —— 挖掘链路的四个阶段
- [安全边界](safety.md) —— `test_dirs()` 是「不许改测试文件」守卫的判据
- 规格原文：`docs/superpowers/specs/2026-07-28-m5-adapter-and-diagnosis-design.md` §4b
