# 项目适配器：把「某种语言的测试工程」翻译给核心循环听

核心循环本身不认识 pytest，也不认识 Maven。它只问适配器几个问题，然后照答案办事。

现在有两个实现：**pytest**（Python）和 **Maven**（Java）。

---

## 目录

- [为什么必须写两个实现](#为什么必须写两个实现)
- [JUnit XML 是公分母，但只解决一半](#junit-xml-是公分母但只解决一半)
- [协议：ProjectAdapter](#协议projectadapter)
- [两个实现的对照表](#两个实现的对照表)
- [locate_source：形状相同，做法不同](#locate_source形状相同做法不同)
- [探测顺序就是探测语义](#探测顺序就是探测语义)
- [怎么加第三个适配器](#怎么加第三个适配器)

---

## 为什么必须写两个实现

**单实现的抽象不是抽象。** 只写 pytest 的话，那层「适配器」会不知不觉长满 pytest 的
假设，而且没有任何东西会提醒你。

这不是理论担忧 —— 加 Maven 的过程里挖出了一串已经存在的裂缝，每一条的失败形态都是
**静默的**：

| 裂缝 | 表现 |
|---|---|
| `eval/mine` 写死 `PytestAdapter()` | Maven 工程 `source_suffixes()` 只认 `.py` → gold_files 恒空 → **产出 0 个任务且不报错**，与「这个仓库最近没有红转绿的提交」无法区分 |
| `preflight` 另存一份适配器注册表 | `MavenAdapter` 登记好了，Maven 工程走到 preflight 照样 abort —— 加了等于没加，两处都不报错 |
| 写入守卫按 `parts[0] in test_dirs` 判 | Maven 的 `test_dirs` 是 `["src/test"]`，首段是 `src` → **最核心的守卫直接放行** |
| `eval/mine` 写死 `"::" not in id` 判文件级 | `::` 是 pytest 的语法，Maven 的 id 一个都没有 → **每一个** Maven id 都被判成文件级 |
| `eval/mine.split_paths` 默认 `(".py",)` | Java 源码全部落空 |

现在这些判定都收在**唯一一处**，加第三个适配器时不会再漏（见
[architecture.md 的「只能有一份」清单](architecture.md#几处只能有一份的实现)）。

---

## JUnit XML 是公分母，但只解决一半

`src/aifix/adapters/junit.py` 一份代码解析 pytest / Maven Surefire / Gradle / Jest 的
报告 —— 那是**解析**这一半。

另一半解决不了：

- **重跑的选择器语法完全不同。** pytest 是 `tests/test_x.py::test_y`，surefire 是
  `demo.CalcTest#testY`。报告里的 `classname` 未必能直接拿去重跑。
- **报告写在哪由构建体系决定。** surefire 只认
  `target/surefire-reports/`，调用方指定不了；pytest 可以 `--junitxml=`。
- **报告可能不止一份。** surefire 每个测试类写一份 `TEST-*.xml`。
- **栈帧的形状完全不同。** Python traceback 由浅入深，Java 堆栈由深入浅。

所以协议里除了「跑测试」这一件真活，还有四个问题要适配器回答。

### JUnit 解析的两个细节

**`ran` 与 `failures` 是两个集合。** 「不在 failures 里」有三种可能：通过了、被删了、
被跳过了。核心循环只关心第一种，但挖任务时把后两种当成「红转绿」会造出无人能通过的
假任务。

**pytest 必须用 `xunit1`。** `xunit2`（pytest 的默认）**不写 `<testcase file=...>`**，
而 `file` 是把报告里的用例还原成可重跑 node id 的唯一可靠依据。所以适配器命令里带
`-o junit_family=xunit1` 覆盖目标项目的配置。已实测（pytest 9.1.1）：xunit1 多出
file/line 两个属性，其余结构完全一致，且无 deprecation 警告。

---

## 协议：ProjectAdapter

`src/aifix/adapters/base.py`。一共九个方法：

```python
class ProjectAdapter(Protocol):
    name: str

    @staticmethod
    def detect(repo: Path) -> bool: ...
    # 这个仓库归我管吗

    def full_test_command(self) -> list[str]: ...
    def scoped_test_command(self, test_ids: list[str]) -> list[str]: ...
    # 跑全量 / 只跑这几个。命令**不接收报告路径** —— 报告写到哪是构建体系自己的事

    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]: ...
    # 报告在哪。返回列表而非单个路径（surefire 每个类一份）
    # 一份都没写出来时返回空列表，不抛 —— 「没跑成」由上层的 require_report 判定

    def test_dirs(self) -> list[str]: ...
    # 哪些目录是测试目录 —— 写入守卫按它判

    def source_suffixes(self) -> tuple[str, ...]: ...
    # 挖任务时哪些后缀算源文件 —— 只收产品代码，不收资源/配置

    def test_selectors(self, test_files: list[str]) -> list[str]: ...
    # 把「commit 改动过的测试文件路径」翻译成 scoped_test_command 认得的选择器

    def make_test_id(self, classname, name, file) -> str: ...
    # 报告里的一条 <testcase> → 可直接重跑的 id

    def is_file_level_id(self, test_id: str) -> bool: ...
    # 这个 id 指的是一整个测试文件/测试类，还是单个用例

    def cases_under(self, file_id: str, test_ids: frozenset[str]) -> set[str]: ...
    # 一个文件级 id 名下有哪些用例 id

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
    # 从失败信息推出嫌疑源码位置，按可疑度排序
```

### 几个「看起来多余」的方法为什么必须存在

**`test_selectors` 不是恒等映射**，只是在 pytest 上恰好长得像：pytest 的选择器就是
路径，surefire 的 `-Dtest=` 只认全限定类名。写死后缀判断的话，Maven 任务的
`test_files` 全是 `.java` → 空 scope → 在做任何事之前就 `return []`，而那副样子与
「这个 commit 没有可用用例」完全一样。

只把后缀放宽同样不成立 —— 路径原样进 `-Dtest=`，surefire **不报错，安静地一个用例都
不跑**。

**`is_file_level_id`**：收集阶段整体失败时报告里发的就是这种 id。

- pytest：测试文件导入失败 → 一条文件级 `<error>`，id 是文件路径
- surefire：测试类初始化失败（`@BeforeAll` 抛异常）→ 一条 `name` 为空的
  `<testcase>`，id 是裸类名

判据必须由适配器给。`eval/mine` 曾写死 `"::" not in id` —— Maven 的 id 一个都没有
`::`，于是每一个都被判成文件级，候选集在复跑那一步被整批清空。

**`scoped` 参数**：pytest 侧全量和复跑必须写成**不同文件**，否则复跑会覆盖掉还要继续
用的全量报告；Maven 侧同一个目录，忽略这个参数即可（命令里的 `clean` 保证目录里只有
本次跑出来的）。

---

## 两个实现的对照表

| | pytest | maven |
|---|---|---|
| `detect` | 有 `pytest.ini` / `tox.ini` / `setup.cfg` / `pyproject.toml` / `conftest.py` 之一，或有 `tests/` 目录 | 根目录有 `pom.xml` |
| 全量命令 | `<python> -B -m pytest -q -p no:cacheprovider -o junit_family=xunit1 --junitxml=.aifix-report.xml` | `mvn -B -q -o clean test -Dmaven.test.failure.ignore=true` |
| 局部命令 | 同上 + `--junitxml=.aifix-recheck.xml <ids...>` | 同上 + `-Dtest=<类>,<类#方法>... -DfailIfNoSpecifiedTests=false` |
| 报告位置 | worktree 根目录下两个固定文件名 | `target/surefire-reports/TEST-*.xml`（每个类一份） |
| `test_dirs` | `["tests", "test"]` | `["src/test"]` |
| `source_suffixes` | `(".py",)` | `(".java",)` |
| 用例 id | `tests/test_x.py::TestC::test_y` | `demo.CalcTest#testY` |
| 文件级 id | `tests/test_x.py`（无 `::`） | `demo.CalcTest`（无 `#`） |
| 跑测试用什么 | 目标项目自己的解释器（见配置） | `mvn`（不走 Python 解释器） |

### 几个命令参数的理由

**pytest 的 `-B` 与 `-p no:cacheprovider`**：不写 `__pycache__`、不写
`.pytest_cache`。理由**不是**「会被扫进交付分支」（交付路径只 `git add` 记账过的路径），
而是未跟踪产物**跨状态存活**：同一个 worktree 会被 `git checkout --force` 在两个提交
之间来回切（挖任务时），而 checkout 不碰未跟踪文件 —— 上一跑留下的东西原样活到下一跑。
压根不写出来的产物，不需要任何人记得去清，也就不存在清漏。

**Maven 的 `clean` 不是为了「干净」，是为了正确**：`mvn test` **不清空**
`target/surefire-reports/`，而 `report_paths` 只看文件系统当前状态。跑一次全量留下
A、B、C，再跑只测 A 的复跑时目录里仍躺着上一轮的 B、C —— 抖动确认据此判定，不报错，
只是判错。代价是每次重新编译（本机约 3 秒）。

**`-o`（离线）**：不打网络。代价是 CI 上要先预热 `~/.m2`，而且**预热必须覆盖实际会跑
的那条命令** —— 实测栽过一次：`mvn test` 不会下载 `maven-clean-plugin`，离线仓库里缺
它时 `clean` 这一阶段当场失败，表现是 surefire 一份报告都没有，被 aifix 报成「测试进程
没能正常跑完」。

**`-Dmaven.test.failure.ignore=true`**：测试一红 `mvn` 就以非 0 退出，而报告那时**已经
写出来了**。不加的话调用方会把退出码读成「没跑成」，而「没跑成」和「跑完了、有红的」
是两种完全不同的结论。

**`-DfailIfNoSpecifiedTests=false`**：id 里的类名对不上（用例被删、被改名）时整个构建
会失败，那会被上层读成「没跑成」，而真相只是少了个用例。

### `make_test_id` 的三种形状（pytest）

都来自实测：

1. **收集错误** —— `classname=""`、`name="tests.test_x"`、`file="tests/test_x.py"`。
   整个文件没能导入，pytest 发的是一条文件级 `<error>`。此时可重跑的 node id 就是文件
   路径本身，不能拼 `::`。
2. **类内测试** —— `classname="tests.test_foo.TestBar"` → `tests/test_foo.py::TestBar::test_baz`。
   classname 尾部超出模块路径的那些段就是类名链（支持嵌套类）。
3. **模块级测试** —— `classname="tests.test_foo"` → `tests/test_foo.py::test_x`。

### Maven 侧一个会出大事的坑

类级失败时 `name` 是空的，**必须退回裸类名，不能拼成 `demo.BootTest#`**。

已实测：`-Dtest=demo.CalcTest#` 被 surefire 读成**没有过滤条件**，整个套件跑一遍，
复跑的报告里躺着无关类的失败。裸类名才是合法选择器。

---

## `locate_source`：形状相同，做法不同

两个实现都返回一个按可疑度排序的 `SourceCandidate` 列表，但拿到候选的路子完全不同。

### `SourceCandidate.origin` 是证据强度，不是来源标签

| origin | 含义 | 强度 |
|---|---|---|
| `traceback` | 失败真的穿过这一帧 | 强 |
| `import` | 测试文件 import 了它 | 弱 —— 「测试用到了这个模块」不等于「缺陷在这个模块」 |

两者必须分得开。合并成一个列表交给下游，`suspect_anchored` 就答不了「这次定位到底是靠
什么」，跨 run 也统计不出「退到 import 之后定位准确率动没动」—— 而那正是引入这条退路时
唯一要回答的问题。

### pytest 侧：两条路

**第一条 —— 解 traceback。** 认两种形状：

```
File "/abs/path/calc.py", line 2, in add       ← Python 原生（--tb=native、嵌套 traceback）
src/mod.py:2: in outer                          ← pytest 的 longrepr（JUnit XML 里实际出现的）
```

第二种有三种收尾：中间帧带函数名、最深帧跟异常类型、入口帧后面什么都没有。行首必须
顶格 —— 缩进的行是被回显的源码，里面出现 `foo.py:3:` 这种字样时不该当成栈帧。

> 早先这里的正则只匹配 Python 原生 traceback，而 **pytest 写进 JUnit 的从来不是那个
> 格式** —— 候选恒空、诊断恒为盲猜。这条 bug 直接影响了历史评测数据的可比性
> （见 [evaluation.md](evaluation.md) 的口径说明）。

**第二条 —— 纯断言失败时退到 import。** 断言失败的 traceback 里只有测试文件那一帧
（被调函数正常返回了，栈上没有它），这是最常见的一类失败。这时候扫测试文件的 import
语句，把仓库内的模块当成候选。

而且会**顺着 re-export 追到符号真正定义的那个文件**：

```
from shopcart import most_expensive
  → src/shopcart/__init__.py        ← 只有一行转发，没有任何逻辑
  → src/shopcart/cart.py            ← 逻辑在这儿
```

停在 `__init__.py` 给出的是一个不含任何逻辑的文件 —— 比模型自己猜好不了多少，
gold_files 也对不上。跳数封顶且带 `seen` 集合，互相转发的两个模块不会把定位转成死循环。

索引 `.py` 文件时会跳过 `.venv` 等目录 —— 这是硬需求不是优化：虚拟环境里有上万个 `.py`
且一个都不是产品代码，漏掉它的话 `from calc import add` 会匹配到 site-packages 里某个
同名模块，模型拿到的候选是**别人的库**。

### Maven 侧：解 Java 堆栈

```
	at demo.Calc.divide(Calc.java:9)
	at java.base/java.util.Objects.requireNonNull(Objects.java:233)   ← 按包名丢掉
```

- **不 reverse**：Python 的 traceback 由浅入深打印，Java 的堆栈由深入浅（栈顶就是抛出
  点），本来就是最深的在最前。
- 按包名前缀丢掉框架帧（`org.junit.`、`java.`、`jdk.`、`sun.`）—— 断言失败时栈顶清一色
  是这些包，不筛的话「最可疑的位置」会指向 JUnit 自己。
- 拿不到行号的帧（`(Native Method)`、`(Unknown Source)`）匹配不上，正好丢掉：没有行号
  的候选对模型没有价值。
- 只映射标准布局 `src/main/java`。多模块（每个 `<module>` 各有自己的 src）是另一件事，
  这里不猜。
- **映射不出真实存在的文件就不给候选** —— `SourceCandidate.path` 会原样进模型的提示词，
  一个不存在的路径会让模型去读空文件甚至凭空造改动。

Java 断言失败的堆栈里往往一条产品代码的帧都没有（被测方法正常返回了，抛异常的是
`assertEquals`），这时候**候选为空是诚实的答案，不是缺陷**。

### 候选之后还有一步：读真实源码

`snippet.around()` 把前三个候选周围各 25 行的**真实源码**读出来，带真实行号，
traceback 指的那一行用 `>` 标出来，一起喂给诊断模型。零 LLM，不多花一个回合。

在这之前，诊断模型判断「根本原因是什么」时看到的只有路径、行号和 traceback 的措辞 ——
那段代码它从未见过，`suspect_lines` 更是纯粹编的。而编出来的行号会原样进入修复模型的
开场白，把它的第一步引向一个具体而错误的位置。

只给前三个：候选列表可以很长，而三段各二十来行已经把单步调用的上下文用得差不多了。

---

## 探测顺序就是探测语义

```python
# nodes/baseline.py —— 全项目唯一的适配器注册表
ADAPTERS = {"maven": MavenAdapter, "pytest": PytestAdapter}
```

**dict 的插入顺序就是探测顺序，改动顺序等于改变探测语义。**

Maven 在前：`MavenAdapter.detect` 要求根目录有 `pom.xml`，是一个具体且几乎不会误判的
信号；`PytestAdapter.detect` 极宽松 —— `pyproject.toml` 或 `tests/` 存在就认领，而
Java 工程的工具链里带 Python 脚本（发版、代码生成、CI 胶水）是常事。

反过来排的后果不是报错而是静默：Maven 工程被判成 pytest 工程，baseline 跑 pytest 命令
收不到任何用例，报告写「0 个失败」。

**通则：detect 越具体的排越前，兜底式的排最后。**

---

## 怎么加第三个适配器

1. **写一个类**实现上面那九个方法，构造函数收一个 `python: str | None = None` 参数
   （即使用不上也要收 —— `adapter_for` 对注册表里每个实现用的是同一行
   `ADAPTERS[name](python=...)`，不收的话会在**取适配器**时 TypeError，而那发生在
   baseline 之前，表现成一次没有测试输出的崩溃）。

2. **登记进 `ADAPTERS`**，位置按「detect 越具体越靠前」放。

3. **检查这几处是不是还成立**（它们是历史上出过裂缝的地方）：

   - [ ] `is_file_level_id` 认得出你这个体系的「文件级/类级失败」形状了吗
   - [ ] `test_selectors` 翻不出来的路径是**丢掉**而不是猜（猜错的选择器在两种适配器上
         都是静默的）
   - [ ] `source_suffixes` 只收产品代码的后缀（gold_files 是定位准确率的判定依据，掺进
         数据文件会稀释它）
   - [ ] 报告写完之后会被清掉吗（陈旧报告被下一跑当成自己的结果 —— 这条在两个现有实现
         里的解法不同：pytest 靠 `_rm_reports`，Maven 靠命令里的 `clean`）
   - [ ] `nodes/baseline._collection_hint` 里要不要为你这个体系加一条「下一步该干什么」
         的提示（现在只有 pytest 那条谈解释器，Maven 走的是通用文案 —— 劝一个 Java
         项目换 Python 解释器是一句假话）

4. **写一个端到端验收**。单元测试证明不了适配器对 —— 它的失败形态几乎全是静默的。
   `.github/workflows/aifix-core-acceptance.yml` 里的 `maven-adapter` job 是个现成的
   模板：造一个带真 bug 的最小工程、跑一次 `aifix run`、用 git 断言分支上真的有东西、
   断言构建产物没进交付分支。

5. **别忘了 `docs/` 和 `evals/`**：`aifix mine` 能不能在你这个体系上挖出任务，是另一个
   要单独验的问题（它需要 `test_selectors` 和 `source_suffixes` 都对）。
