# 项目适配器：把「某种语言的测试工程」翻译给核心循环听

核心循环本身不认识 pytest，也不认识 Maven。它只问适配器几个问题，然后照答案办事。

现在有三个实现：**pytest**（Python）、**Maven**（Java）和 **vitest**（JS/TS 前端）。

---

## 目录

- [为什么必须写多个实现](#为什么必须写多个实现)
- [JUnit XML 是公分母，但只解决一半](#junit-xml-是公分母但只解决一半)
- [协议：ProjectAdapter](#协议projectadapter)
- [三个实现的对照表](#三个实现的对照表)
- [locate_source：形状相同，做法不同](#locate_source形状相同做法不同)
- [探测顺序就是探测语义](#探测顺序就是探测语义)
- [怎么加下一个适配器](#怎么加下一个适配器)

---

## 为什么必须写多个实现

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

    def is_test_path(self, path: str) -> bool: ...
    # 这个路径是不是测试文件。「不许改测试文件」那道守卫的唯一判据
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

**`is_test_path` 为什么不能是 `test_dirs()`**：目录列表等于断言「测试都住在某个目录
下」，而这个前提对 vitest 不成立 —— JS 生态的主流约定是测试与源码**同目录**
（`src/components/ChatView.test.tsx` 与 `src/components/ChatView.tsx` 并排），靠后缀
区分。

拿目录表达它只能二选一地错：

| 返回 | 后果 |
|---|---|
| `[]` | 守卫**静默放行**，修复阶段的 agent 可以直接改掉自己的判卷标准 |
| `["src"]` | 整个源码树被判成测试，fixer 什么都改不了 |

所以判据必须由适配器给，而且必须是**谓词**。pytest 与 Maven 的实现就是
`under_dirs(path, self.test_dirs())`，与改造前逐字节相同。

`test_dirs()` 保留但用途收窄成两件事：写进 reproducer 的提示词（新测试放哪），以及挖
任务时拆分改动路径。**判断「是不是测试」一律走谓词。**

同一个谓词还用在 `reproducer._path_is_safe`（校验模型给的测试路径）。这不只是复用 ——
它保证「校验通过的复现测试」必然「fixer 改不动」。两处各用各的判据就会有一条缝，落在
缝里的文件校验说它是测试、守卫说它不是。

这道守卫**已经静默失效过一次**：Maven 的 `["src/test"]` 遇上当时只比首段的实现，首段
是 `src`，每一次改测试都被放行，且不报错、报告照样显示绿。

**`scoped` 参数**：pytest 侧全量和复跑必须写成**不同文件**，否则复跑会覆盖掉还要继续
用的全量报告；Maven 侧同一个目录，忽略这个参数即可（命令里的 `clean` 保证目录里只有
本次跑出来的）。

---

## 三个实现的对照表

| | pytest | maven | vitest |
|---|---|---|---|
| `detect` | 有 `pytest.ini` / `tox.ini` / `setup.cfg` / `pyproject.toml` / `conftest.py` 之一，或有 `tests/` 目录 | 根目录有 `pom.xml` | 根目录 `package.json` 的依赖里直接列了 `vitest` |
| 全量命令 | `<python> -B -m pytest -q -p no:cacheprovider -o junit_family=xunit1 --junitxml=.aifix-report.xml` | `mvn -B -q -o clean test -Dmaven.test.failure.ignore=true` | `node_modules/.bin/vitest run --reporter=junit --outputFile=.aifix-report.xml` |
| 局部命令 | 同上 + `--junitxml=.aifix-recheck.xml <ids...>` | 同上 + `-Dtest=<类>,<类#方法>... -DfailIfNoSpecifiedTests=false` | 同上 + `<文件...> -t "^名字$\|^名字$"`（见下） |
| 报告位置 | worktree 根目录下两个固定文件名 | `target/surefire-reports/TEST-*.xml`（每个类一份） | `--outputFile` 指定，**相对 `--root` 解析**（不是相对 cwd） |
| `test_dirs` | `["tests", "test"]` | `["src/test"]` | `["src"]`（只用于提示词与挖任务，**判定走 `is_test_path`**） |
| `source_suffixes` | `(".py",)` | `(".java",)` | `.ts` / `.tsx` / `.js` / `.jsx` / `.vue` / `.svelte`（不含 `.d.ts`） |
| 用例 id | `tests/test_x.py::TestC::test_y` | `demo.CalcTest#testY` | `src/lib/calc.test.ts::calc > 断言失败` |
| 文件级 id | `tests/test_x.py`（无 `::`） | `demo.CalcTest`（无 `#`） | `src/broken.test.ts`（无 `::`） |
| 跑测试用什么 | 目标项目自己的解释器（见配置） | `mvn`（不走 Python 解释器） | 工程自己的 `node_modules/.bin/vitest`（不走 Python 解释器） |

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

### vitest 侧：报告里的 id 有四处不能直接拿去用

每一处错了都**不报错** —— vitest 只是安静地跑 0 条，或者多跑几条。

**1. 分隔符不是同一个。** 报告的 `<testcase name>` 用 ` > ` 连接 describe 层级，而
`-t` 匹配的是**空格**连接的那份。实测：

```
-t "外层 > 内层 > 用例"   → 跑了 0 条
-t "外层 内层 用例"       → 命中
```

**2. `-t` 收的是正则，不是字面量。** 名字里带 `(括号)` 不转义就匹配不到（被当成捕获
组）。转义**不能用 Python 的 `re.escape`** —— 它把空格也转成 `\ `，而 JS 正则在 `u`
标志下把那当作非法的身份转义。只转 JS 真正认的那些元字符。

**3. 必须加 `^...$` 锚点。** 实测 `-t "外层 前缀"` 同时跑了 `外层 > 前缀` 和
`外层 > 前缀加长` —— 复跑多跑用例会污染 flaky 确认与红检的判定集合。

**4. 文件级失败的形状是 `classname == name == 文件路径`。** 整个文件没能加载时 vitest
就发这么一条，`name` 被填成文件路径本身。拼成 `文件::文件` 会得到一个跑不起来的 id。

另外，**有文件级 id 时整个不发 `-t`**：那种 id 名下没有用例名可写进选择分支，而发一个
匹配不到它的 `-t` 会让它被跳过 —— 于是「这个文件整体加载失败」在复跑结果里消失。多跑
几条用例是可承受的，把一整类失败跑丢不是。

### vitest 的命令为什么不走 `npx`、也不走项目的 `test` 脚本

`npm run test` 的内容是项目自己定的。写成 `vitest`（不带 `run`）就是 **watch 模式** ——
进程永远不退出，整个 run 挂到墙钟闸响。而 `vitest` 与 `vitest run` 只差一个词，前者
恰恰是 vitest 文档里最常出现的写法。

不走 `npx` 是因为包不在时它会尝试联网安装，在沙箱里那是一次几十秒的超时，而不是一条
清楚的错误。

直接调 `node_modules/.bin/vitest`。相对路径的可执行文件在沙箱里可用 —— 已实测。

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

### vitest 侧：解 JS 栈

帧长成 ` ❯ inner src/lib/calc.ts:2:20`，函数名那一段可缺（入口帧就没有）。

**由深入浅**，与 Java 一致、与 Python 相反 —— 所以和 Maven 一样**不 reverse**。弄反的
后果是「最可疑的位置」指向测试文件里那行调用，而不是真正抛异常的产品代码。

正则锚定到行尾的 `:行:列`，不按空格切：路径里可以有空格，而函数名与路径之间也是空格，
只按空格切会在两种情况下各切错一次。

`node_modules` 里的帧要丢掉 —— 加载失败时栈顶就是 vite 自己的 `loadAndTransform`。
把框架内部文件递给模型，只会让它去读一个与缺陷无关的几万行文件。

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
ADAPTERS = {"maven": MavenAdapter, "vitest": VitestAdapter,
            "pytest": PytestAdapter}
```

**dict 的插入顺序就是探测顺序，改动顺序等于改变探测语义。**

Maven 在前：`MavenAdapter.detect` 要求根目录有 `pom.xml`，是一个具体且几乎不会误判的
信号；`PytestAdapter.detect` 极宽松 —— `pyproject.toml` 或 `tests/` 存在就认领，而
Java 工程的工具链里带 Python 脚本（发版、代码生成、CI 胶水）是常事。

反过来排的后果不是报错而是静默：Maven 工程被判成 pytest 工程，baseline 跑 pytest 命令
收不到任何用例，报告写「0 个失败」。

vitest 排在 pytest 之前同理，而且更要紧：它要求根目录 `package.json` 的依赖里**直接**
列了 vitest，同样具体。前后端同仓的工程根目录往往两样都有（`pyproject.toml` +
`package.json`），反过来排的话前端工程会被判成 pytest 工程，然后 baseline 收不到任何
用例、报告写「0 个失败」。

**通则：detect 越具体的排越前，兜底式的排最后。**

### 一次 run 可以跑两套，但要**显式开**

前后端同仓的工程（Python 后端 + vitest 前端）设一个环境变量就两套都跑：

```bash
export AIFIX_ADAPTERS=pytest,vitest
```

不设的话**只用探测到的第一个** —— 与加多适配器之前逐字节相同的行为。

**为什么不自动「探测到几个就跑几个」**：`PytestAdapter.detect` 极宽松（有 `tests/`
或 `pyproject.toml` 就认领），而 Java 工程的工具链里带 Python 脚本是常事。自动全跑的
话，那类仓库会凭空多跑一套 pytest、收不到任何用例，然后被 `require_report` 判成
「测试没跑成」当场中止 —— **一个今天能正常工作的仓库在升级之后打不开**。

而「该不该两套都跑」没有可靠的自动判据：前后端同仓（`pyproject.toml` +
`package.json`）与 Java 带 Python 胶水，在探测那一层长得一模一样。分不清就不猜。

只跑一套时的后果值得说清楚：另一侧的用例**一条都不执行** —— 那不是「通过」，是不
存在。baseline 看不见它们，verify 的三态比较也就永远不会因为它们变红而判 WORSE。

### 两套并存靠的是「出处记账」

`FailureSet.owner` 记下每条 id 是**哪个适配器**跑出来的。

判据必须记账，**不能按 id 形状猜**：这个仓库为按形状猜栽过一次（`eval/mine` 写死
`"::" not in id`，Maven 的 id 一个都没有 `::`，于是每一个都被判成文件级、候选集被
整批清空，且不报错）。而 vitest 的 id 恰好也用 `::`，再猜一次只会把两者混在一起。

**通过的用例也要记账**：复跑一条当前没红的用例（flaky 确认就在做这件事）同样要知道
问谁，而它不在 `failures` 里。

三处按记账办事：

| 谁 | 用它干什么 |
|---|---|
| `adapter_for_test` | detect / fix 拿到一条 id，问它归谁 |
| `run_scoped` 的 `_dispatch` | 把一批 id 分给各自的适配器，**只跑点到名的那几套** |
| `file_level_ids` | 「这是不是文件级 id」也是各家语法各不相同 |

记账缺失而当前有多个适配器时**抛异常，不猜**。猜错在三种适配器上都是静默的：id 进了
另一套体系的命令行，那套体系不报错，只是一个用例都不跑、写出 `tests="0"` 的报告 ——
而复跑「跑了个空」会被 `filter_flaky` 读成「重跑就绿」，于是真的被补丁弄红的用例被
划进抖动、从判定里剔除。

**`verify.compare()` 一行都不用改。** 它是纯集合运算，把两份 `FailureSet` 并起来喂
进去就成立 —— 这正是「三态判定不该知道语言」那条设计在多适配器上兑现的地方。

### worktree 里没有 `node_modules`

worktree 只含**被 git 跟踪**的文件，而依赖目录都不被跟踪。三个适配器各自的处境
不一样：

| | 依赖在哪 | worktree 里要补什么 |
|---|---|---|
| pytest | worktree **之外**的那个解释器（绝对路径） | 什么都不用 |
| maven | 本机的 `~/.m2` | 什么都不用 |
| vitest | 按 cwd 向上找 `node_modules` | **要补** |

所以协议上有一个 `prepare(worktree)`，前两个是空实现。

vitest 那条的做法是**建真目录 + 逐个子项软链**，不是把整个 `node_modules` 软链
过去。这不是洁癖：实测 vitest 跑完会往 `node_modules/.vite` 写依赖预构建缓存，
整个目录软链过去的话那些写**落在源仓库里** —— 而「agent 的一切改动都发生在
worktree，主工作区绝不被触碰」是这个项目的地基。

逐个子项软链之后，读走源仓库、写落在 worktree。255 个软链是毫秒级，而 `npm ci`
实测 12 秒（还得有缓存或网络）。有一条真跑的测试钉住「`.vite` 出现在 worktree 侧、
源仓库侧没有」——把它改成整个目录软链，那条会红。

### `package.json` 不一定在根目录

前后端同仓的工程前端几乎总在 `web/` / `frontend/` / `client/` 这类一级目录下。
`find_pkg_dir` 先看根、再看**一层**子目录，不写死名字（那份名单永远不全，漏掉一个
的表现是「这个仓库没有前端」——与真的没有完全无法区分）。

只下一层不递归：再深就更可能是 monorepo 的某个 package，而「哪个才是要跑的那套」
不是探测能回答的问题。子目录按名字排序取第一个命中的，让结果可复现。

前端在子目录时**两个坐标系要来回翻**：报告里的路径相对 `--root`，而适配器对外
一律说仓库相对路径（那是核心循环、`is_test_path` 与交付记账共同的坐标系）。

### 还没补的窟窿

`aifix reproduce` 与 issue 那条路仍然**只取第一个适配器**：一条复现测试只能是一种
语言，而「该写哪一侧」今天没有判据。

前后端同仓的工程里报另一侧的缺陷时，模型会拿错语言写测试，而红检只会说「这条测试
没红」—— 一句指错方向的话。补它要让 reproducer 自己判断，那需要先有数据说明它判得
准不准。

---

## 怎么加下一个适配器

1. **写一个类**实现上面那十二个方法，构造函数收一个 `python: str | None = None` 参数
   （即使用不上也要收 —— `adapter_for` 对注册表里每个实现用的是同一行
   `ADAPTERS[name](python=...)`，不收的话会在**取适配器**时 TypeError，而那发生在
   baseline 之前，表现成一次没有测试输出的崩溃）。

2. **登记进 `ADAPTERS`**，位置按「detect 越具体越靠前」放。登记之后
   `test_adapter_matches_the_protocol_member_for_member` 会自动把它算进去 ——
   那条测试的参数化是跟着注册表走的。**它一度写死成名单**，于是 VitestAdapter
   加进去之后整整一轮没被检查到，而那不报错。

3. **检查这几处是不是还成立**（它们是历史上出过裂缝的地方）：

   - [ ] `is_file_level_id` 认得出你这个体系的「文件级/类级失败」形状了吗
   - [ ] `test_selectors` 翻不出来的路径是**丢掉**而不是猜（猜错的选择器在两种适配器上
         都是静默的）
   - [ ] `source_suffixes` 只收产品代码的后缀（gold_files 是定位准确率的判定依据，掺进
         数据文件会稀释它）
   - [ ] 报告写完之后会被清掉吗（陈旧报告被下一跑当成自己的结果 —— 这条在两个现有实现
         里的解法不同：pytest 靠 `_rm_reports`，Maven 靠命令里的 `clean`）
   - [ ] `is_test_path` 用的是**你这个体系真正的判据**吗 —— vitest 靠后缀而不是
         目录，照抄 `under_dirs(path, test_dirs())` 会让守卫静默失效
   - [ ] 报告里的 id **能直接当选择器**吗（多半不能）。vitest 的 ` > ` 与 `-t` 认的
         空格不是同一个字符串，surefire 的 `-Dtest=` 只认全限定类名 —— 两处都是
         「不报错，安静地跑个空」
   - [ ] `nodes/baseline._collection_hint` 里要不要为你这个体系加一条「下一步该干什么」
         的提示（现在只有 pytest 那条谈解释器，Maven 走的是通用文案 —— 劝一个 Java
         项目换 Python 解释器是一句假话）

4. **写一个端到端验收**。单元测试证明不了适配器对 —— 它的失败形态几乎全是静默的。
   `.github/workflows/aifix-core-acceptance.yml` 里的 `maven-adapter` job 是个现成的
   模板：造一个带真 bug 的最小工程、跑一次 `aifix run`、用 git 断言分支上真的有东西、
   断言构建产物没进交付分支。

5. **别忘了 `docs/` 和 `evals/`**：`aifix mine` 能不能在你这个体系上挖出任务，是另一个
   要单独验的问题（它需要 `test_selectors` 和 `source_suffixes` 都对）。
