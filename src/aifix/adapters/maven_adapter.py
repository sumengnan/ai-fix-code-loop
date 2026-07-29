from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .base import Failure, SourceCandidate

# Java 栈帧：`\tat demo.Calc.divide(Calc.java:9)`。
# `java.base/` 那段是 JDK 9+ 的模块前缀，只出现在 JDK 自己的帧上，可选。
# 拿不到行号的帧（`(Native Method)`、`(Unknown Source)`）匹配不上，正好丢掉：
# 没有行号的候选对模型没有价值。
_FRAME = re.compile(
    r"\bat (?:[\w.]+/)?(?P<cls>[\w.$]+)\.(?P<meth>[\w$<>]+)"
    r"\((?P<file>[\w$]+\.java):(?P<line>\d+)\)")

# 按包名前缀丢掉不属于被测项目的帧。下面的存在性检查其实已经能挡住它们，
# 但一条 Java 堆栈动辄几十帧且大半是框架的，先按前缀筛掉可以少 stat 一大半；
# 更要紧的是断言失败时栈顶清一色是这些包，不筛的话「最可疑的位置」会指向
# JUnit 自己。
_FOREIGN = ("org.junit.", "org.opentest4j.", "java.", "jdk.", "sun.")

# 只映射标准布局的产品代码。多模块（`<module>` 各有自己的 src/main/java）
# 是另一件事，这里不猜。
_MAIN_SRC = "src/main/java"
_TEST_SRC = "src/test/java"


class MavenAdapter:
    name = "maven"

    @staticmethod
    def detect(repo: Path) -> bool:
        return (repo / "pom.xml").is_file()

    # clean 不是为了「干净」，是为了正确：`mvn test` **不清空**
    # target/surefire-reports/，而 report_paths 只看文件系统当前状态。跑一次
    # 全量留下 A、B、C，再跑只测 A 的复跑，目录里仍躺着上一轮的 B、C，
    # parse_junit 会把上一轮的失败算成这一轮的 —— flaky 确认据此判定，
    # 不报错，只是判错。run_full_suite/run_scoped 的 finally 里确实会删报告，
    # 但只删 report_paths 当时返回的那些，任何一次异常退出都会留下残骸；
    # 把这件事挂在别人的 finally 上不成立。代价是每次重新编译（本机约 3 秒）。
    #
    # -o：离线，不打网络。-B：非交互，日志不带 ANSI 和进度条。-q：只留 ERROR。
    #
    # -Dmaven.test.failure.ignore=true：测试一红 mvn 就以非 0 退出，而报告
    # 那时**已经写出来了**。不加的话调用方会把退出码读成「没跑成」，而
    # 「没跑成」和「跑完了、有红的」在这个项目里是两种完全不同的结论。
    _BASE = ["mvn", "-B", "-q", "-o", "clean", "test",
             "-Dmaven.test.failure.ignore=true"]

    def full_test_command(self) -> list[str]:
        return list(self._BASE)

    def scoped_test_command(self, test_ids: list[str]) -> list[str]:
        # -DfailIfNoSpecifiedTests=false：id 里的类名对不上（用例被删、被改名）
        # 时整个构建会失败，那会被上层读成「没跑成」，而真相只是少了个用例。
        return [*self._BASE, "-Dtest=" + ",".join(test_ids),
                "-DfailIfNoSpecifiedTests=false"]

    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]:
        """surefire 每个测试类写一份 TEST-<全限定类名>.xml，只能整目录取。

        scoped 无意义：报告目录由 surefire 定死，全量和复跑写在同一处。
        两者不会互相覆盖 —— 命令里的 clean 保证目录里只有本次跑出来的。
        """
        return sorted(Path(worktree).glob("target/surefire-reports/TEST-*.xml"))

    def test_dirs(self) -> list[str]:
        return ["src/test"]

    def source_suffixes(self) -> tuple[str, ...]:
        # 只有 `.java`。pom.xml 的改动确实能让测试转红转绿（依赖版本、
        # 编译级别），但它不是 locate_source 能指向的东西 —— 这个适配器只把
        # 栈帧映射到 src/main/java 下的 `.java`，把 pom.xml 塞进 gold_files
        # 等于给 Detector 记一个它按设计就拿不到的分。
        return (".java",)

    def test_selectors(self, test_files: list[str]) -> list[str]:
        """`src/test/java/demo/CalcTest.java` → `demo.CalcTest`。

        `-Dtest=` 认的是全限定类名，不认路径。只给类名不带 `#方法` 是合法的，
        跑整个类 —— 已实测（surefire 3.2.5）：`-Dtest=demo.CalcTest` 只跑
        CalcTest 的用例，同工程的 OtherTest 一个都没跑到。

        非 `.java`（src/test/resources 下的测试资源）与非标准布局
        （多模块的 `<module>/src/test/java/...`）一律丢掉，与 locate_source
        只映射 `src/main/java` 是同一条线：这里猜出来的类名不会让 mvn 报错，
        surefire 只是安静地一个用例都不跑，而那副样子与「这个 commit 没有
        可用用例」完全一样。
        """
        out: list[str] = []
        for p in test_files:
            pp = PurePosixPath(p)
            if pp.suffix != ".java":
                continue
            try:
                rel = pp.relative_to(_TEST_SRC)
            except ValueError:
                continue
            out.append(".".join([*rel.parent.parts, rel.stem]))
        return out

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """surefire 的 -Dtest= 选择器语法就是 `全限定类名#方法名`。

        file 一定是 None：surefire 的 <testcase> 不写 file 属性（pytest 的
        xunit1 才写）。留着这个参数是因为 parse_junit 按位置传三个值。

        **name 为空 = 类级失败**，退回裸类名。已实测（surefire 3.2.5 /
        JUnit 5.10.2）：`@BeforeAll` 抛异常时整个类只发一条
        `<testcase name="" classname="demo.BootTest">` 带 `<error>`，两个
        `@Test` 方法一条都不发 —— 这是 pytest 侧「文件导入失败发一条文件级
        <error>」的对应物。拼成 `demo.BootTest#` 会出大事：已实测
        `-Dtest=demo.CalcTest#` 被 surefire 读成**没有过滤条件**，整个套件
        跑一遍，复跑的报告里躺着无关类的失败。裸类名才是合法选择器
        （`-Dtest=demo.CalcTest` 只跑那个类）。

        classname 为空时退回裸方法名：surefire 没见过这种报告，但拼出来的
        `#addWorks` 是个 -Dtest= 认不出的 id，那会静默地一个用例都不跑。
        """
        if not classname:
            return name
        return f"{classname}#{name}" if name else classname

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 Java 堆栈抽出 src/main/java 下的帧，最深的排最前。

        不像 PytestAdapter 那样 reverse：Python 的 traceback 由浅入深打印，
        Java 的堆栈由深入浅（栈顶就是抛出点），本来就是最深的在最前。

        映射不出真实存在的文件就不给候选 —— SourceCandidate.path 会原样进
        模型的提示词，一个不存在的路径会让模型去读空文件甚至凭空造改动。
        被丢掉的主要是测试类自己的帧（它在 src/test/java）：Java 断言失败的
        堆栈里往往一条产品代码的帧都没有（被测方法正常返回了，抛异常的是
        assertEquals），这时候候选为空是诚实的答案，不是缺陷。
        """
        repo = Path(repo)
        out: list[SourceCandidate] = []
        for m in _FRAME.finditer(failure.trace or ""):
            cls = m.group("cls")
            if cls.startswith(_FOREIGN):
                continue
            # 包名 = 全限定类名去掉最后一段（简单类名）。内部类 `Calc$Inner`
            # 也落在同一段里，去掉后包名照样对，文件名直接用帧里给的。
            pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
            rel = Path(_MAIN_SRC, *(pkg.split(".") if pkg else ()),
                       m.group("file"))
            if not (repo / rel).is_file():
                continue
            out.append(SourceCandidate(
                path=rel.as_posix(),
                line=int(m.group("line")),
                frame=f"{cls}.{m.group('meth')}",
            ))
        return out
