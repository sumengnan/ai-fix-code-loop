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
| `full_test_command() -> list[str]` | `python -B -m pytest -q -p no:cacheprovider -o junit_family=xunit1 --junitxml=.aifix-report.xml` | `mvn -B -q -o clean test -Dmaven.test.failure.ignore=true` |
| `scoped_test_command(test_ids) -> list[str]` | 同上，换 `--junitxml=.aifix-recheck.xml`，末尾追加 id | 追加 `-Dtest=<逗号连接>` 与 `-DfailIfNoSpecifiedTests=false` |
| `report_paths(worktree, scoped=False) -> list[Path]` | 单份：`.aifix-report.xml` 或 `.aifix-recheck.xml` | 整目录：`target/surefire-reports/TEST-*.xml` |

**命令不接收报告路径** —— 报告写到哪里是构建体系自己的事，Maven surefire 只认 `target/surefire-reports/`，调用方指定不了。

几个非显然的选项，每一个都是踩出来的：

- pytest 的 `-o junit_family=xunit1`：xunit2（pytest 的默认）**不写** `<testcase file=...>`，而 `file` 是把 junit 报告里的用例还原成可重跑 node id 的唯一可靠依据
- pytest 的 `-B` / `-p no:cacheprovider`：不写 `__pycache__` 与 `.pytest_cache`。理由**不是**「会被扫进交付分支」（`Worktree.commit` 只 add 记账过的路径），而是未跟踪产物**跨状态存活** —— 同一个 worktree 会被 `git checkout --force` 在 `C^` 和 `C` 之间来回切，而 checkout 不碰未跟踪文件
- Maven 的 `clean`：`mvn test` **不清空** `target/surefire-reports/`，而 `report_paths` 只看文件系统当前状态。跑一次全量留下 A、B、C，再跑只测 A 的复跑，目录里仍躺着上一轮的 B、C —— flaky 确认据此判定，**不报错，只是判错**
- Maven 的 `-Dmaven.test.failure.ignore=true`：测试一红 `mvn` 就以非 0 退出，而报告那时**已经写出来了**。不加的话调用方会把退出码读成「没跑成」，而「没跑成」和「跑完了、有红的」在这个项目里是两种完全不同的结论
- `scoped` 参数是决定性的，不是排版细节：pytest 侧两份报告必须**不同名**（复跑会覆盖掉还要继续用的全量报告）；Maven 侧由 surefire 写在同一个目录，忽略这个参数即可

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

`is_file_level_id` 问的是「这个 id 指的是一整个测试文件 / 测试类，而不是单个用例吗」。**收集阶段整体失败时报告里发的就是这种 id**：pytest 的测试文件导入失败发一条文件级 `<error>`，surefire 的测试类初始化失败发一条 `name` 为空的 `<testcase>`。挖任务时「测试文件在 `C^` 起不来、在 `C` 正常」是一整类候选 —— 实测本仓库 65 个候选 commit 里 32 个是那个形状。

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

## 相关文档

- [架构](architecture.md) —— 适配器在哪些节点被调用
- [评测](evaluation.md) —— 挖掘链路的四个阶段
- [安全边界](safety.md) —— `test_dirs()` 是「不许改测试文件」守卫的判据
- 规格原文：`docs/superpowers/specs/2026-07-28-m5-adapter-and-diagnosis-design.md` §4b
