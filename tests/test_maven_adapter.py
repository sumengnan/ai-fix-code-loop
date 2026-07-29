"""MavenAdapter —— 断言 surefire 真实写出来的形状。

夹具真跑 `mvn`：手写 XML 只能证明我们理解得自洽，证明不了 surefire 真的这么写。
`mvn` 慢（首次约 8 秒，之后约 3 秒），所以所有跑 mvn 的夹具都是 module 级，
整个文件一共只起 3 次 mvn。
"""
from __future__ import annotations

import inspect
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aifix.adapters.base import Failure
from aifix.adapters.junit import parse_junit
from aifix.adapters.maven_adapter import MavenAdapter

pytestmark = pytest.mark.skipif(
    shutil.which("mvn") is None, reason="本机没有 mvn，跳过 Maven 适配器测试")

# surefire 与 junit-jupiter 都钉死版本：Maven 3.9 默认生命周期绑的是
# surefire 2.12.4，那个版本跑不了 JUnit 5，不钉版本夹具会静默一个用例都不跑。
# 两个版本本机 ~/.m2 里都有，配合适配器命令里的 -o 可以完全离线。
_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.10.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
      </plugin>
    </plugins>
  </build>
</project>
"""

# divide 的存在是有目的的：Java 里断言失败的堆栈**不含**被测方法的帧
# （add 正常返回了，抛异常的是 assertEquals），要拿到一条指向 src/main/java
# 的帧，必须有一个真的在产品代码里抛出来的异常。
# divide 又特地多套一层 doDivide：只有两条产品代码的帧才能验证排序 ——
# 一条帧的话，把顺序反过来的实现也照样通过。
_CALC = """package demo;

public class Calc {
    public int add(int a, int b) {
        return a - b;   // bug: should be a + b
    }

    public int divide(int a, int b) {
        return doDivide(a, b);
    }

    private int doDivide(int a, int b) {
        return a / b;
    }
}
"""

_CALC_TEST = """package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void addWorks() {
        assertEquals(3, new Calc().add(1, 2));
    }

    @Test
    void divideBlowsUp() {
        assertEquals(0, new Calc().divide(1, 0));
    }

    @Test
    void alsoPasses() {
        assertEquals(0, new Calc().add(1, 1));
    }
}
"""

# 第二个测试类：surefire 每个类写一份报告，陈旧报告那条测试需要两份才有区分度。
_OTHER_TEST = """package demo;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class OtherTest {
    @Test
    void alwaysPasses() {
        assertTrue(true);
    }
}
"""


def _write_project(root: Path) -> Path:
    (root / "src/main/java/demo").mkdir(parents=True)
    (root / "src/test/java/demo").mkdir(parents=True)
    (root / "pom.xml").write_text(_POM, encoding="utf-8")
    (root / "src/main/java/demo/Calc.java").write_text(_CALC, encoding="utf-8")
    (root / "src/test/java/demo/CalcTest.java").write_text(
        _CALC_TEST, encoding="utf-8")
    (root / "src/test/java/demo/OtherTest.java").write_text(
        _OTHER_TEST, encoding="utf-8")
    return root


def _reports(repo: Path) -> list[str]:
    return sorted(p.name
                  for p in (repo / "target/surefire-reports").glob("TEST-*.xml"))


@pytest.fixture(scope="module")
def maven_repo(tmp_path_factory) -> Path:
    """一个没跑过的干净工程。不碰 mvn 的测试用这个。"""
    return _write_project(tmp_path_factory.mktemp("mvn_pristine"))


@pytest.fixture(scope="module")
def full_run(tmp_path_factory) -> dict:
    """跑一次全量。这份目录之后没人改，报告一直留着给多个测试读。"""
    repo = _write_project(tmp_path_factory.mktemp("mvn_full"))
    proc = subprocess.run(MavenAdapter().full_test_command(), cwd=repo,
                          capture_output=True, text=True)
    return {"repo": repo, "proc": proc}


@pytest.fixture(scope="module")
def scoped_run(tmp_path_factory) -> dict:
    repo = _write_project(tmp_path_factory.mktemp("mvn_scoped"))
    proc = subprocess.run(
        MavenAdapter().scoped_test_command(["demo.CalcTest#addWorks"]),
        cwd=repo, capture_output=True, text=True)
    return {"repo": repo, "proc": proc}


@pytest.fixture(scope="module")
def class_selector_run(tmp_path_factory) -> dict:
    """只给类名、不带 `#方法` 的 scoped 跑 —— test_selectors 产出的形状。"""
    repo = _write_project(tmp_path_factory.mktemp("mvn_class_sel"))
    proc = subprocess.run(
        MavenAdapter().scoped_test_command(["demo.CalcTest"]),
        cwd=repo, capture_output=True, text=True)
    return {"repo": repo, "proc": proc}


@pytest.fixture(scope="module")
def stale_run(tmp_path_factory, full_run) -> dict:
    """把跑完全量的目录（含 target/）整个复制过来，再跑一次 scoped。

    模拟 _rm_reports 没能执行的情形：它只删 report_paths 当时返回的那些，
    任何一次异常退出（超时被杀 / 沙箱失败）都会把上一轮的报告留在原地。
    复制而不是重跑一次全量，是为了省掉一次 mvn。
    """
    repo = tmp_path_factory.mktemp("mvn_stale") / "proj"
    shutil.copytree(full_run["repo"], repo)
    before = _reports(repo)
    proc = subprocess.run(
        MavenAdapter().scoped_test_command(["demo.OtherTest#alwaysPasses"]),
        cwd=repo, capture_output=True, text=True)
    return {"repo": repo, "before": before, "proc": proc}


def _failures(full_run: dict) -> dict[str, Failure]:
    a = MavenAdapter()
    return parse_junit(a.report_paths(full_run["repo"]), a.make_test_id).failures


# ---------- 不需要 mvn 的部分 ----------

def test_detect_by_pom(maven_repo):
    assert MavenAdapter.detect(maven_repo) is True


def test_detect_rejects_a_dir_without_pom(tmp_path):
    assert MavenAdapter.detect(tmp_path) is False


def test_commands_match_the_protocol_signature():
    """命令不接收报告路径：surefire 只认 target/surefire-reports/，调用方指定不了。"""
    a = MavenAdapter()
    assert inspect.signature(a.full_test_command).parameters == {}
    assert list(inspect.signature(a.scoped_test_command).parameters) == ["test_ids"]


def test_scoped_command_carries_the_ids_and_tolerates_unknown_classes():
    """-DfailIfNoSpecifiedTests=false：类名对不上时不至于让整个构建失败 ——
    构建失败会被上层读成「没跑成」，而这只是一个用例被删了。"""
    cmd = MavenAdapter().scoped_test_command(
        ["demo.CalcTest#addWorks", "demo.OtherTest#alwaysPasses"])
    assert "-Dtest=demo.CalcTest#addWorks,demo.OtherTest#alwaysPasses" in cmd
    assert "-DfailIfNoSpecifiedTests=false" in cmd


def test_make_test_id_uses_surefire_selector_syntax():
    """id 要能直接喂给 -Dtest=，surefire 的语法是 类#方法。"""
    assert MavenAdapter().make_test_id("demo.CalcTest", "addWorks", None) == \
        "demo.CalcTest#addWorks"


def test_test_dirs_is_the_maven_standard_layout():
    assert MavenAdapter().test_dirs() == ["src/test"]


def test_source_suffixes_is_java_only():
    """gold_files 衡量的是定位**源文件**的能力。

    `.xml`（pom）、`.properties`（资源）都躺在 Maven 工程里，收进来只会稀释
    这个指标；locate_source 也只映射 src/main/java 下的 `.java`。
    """
    assert MavenAdapter().source_suffixes() == (".java",)


def test_test_selectors_translate_paths_into_class_names():
    """`src/test/java/demo/CalcTest.java` → `demo.CalcTest`。

    这一步不能省成「路径原样递给 scoped_test_command」：surefire 的
    `-Dtest=` 认的是全限定类名，喂路径进去不报错，只是一个用例都不跑。
    只给类名不带 `#方法` 是合法的，跑整个类（本文件的 test_selector_for_a_whole_class_really_runs_it
    用真 mvn 钉住了这一点）。

    映射不出标准布局的一律丢掉，不猜：猜出来的类名同样不报错、同样一个
    用例都不跑，而那时看起来像「这个 commit 没有可用用例」。
    """
    got = MavenAdapter().test_selectors([
        "src/test/java/demo/CalcTest.java",
        "src/test/java/RootTest.java",
        "src/test/resources/fixture.json",      # 测试资源，不是类
        "svc/src/test/java/demo/ModTest.java",  # 多模块，本适配器不映射
    ])
    assert got == ["demo.CalcTest", "RootTest"], got


def test_test_selectors_reject_the_pytest_shaped_input():
    """反向：`.py` 路径不是 Java 测试类，不能被放行。

    没有这一条，一个「原样返回 test_files」的实现能过上面那条的前两项吗 ——
    不能，但一个「只要有后缀就拼个类名」的实现能。
    """
    assert MavenAdapter().test_selectors(["tests/test_calc.py"]) == []


def test_report_paths_is_empty_when_nothing_ran(tmp_path):
    """报告缺失返回空列表，不抛 —— require_report 那一层才负责判定。"""
    assert MavenAdapter().report_paths(tmp_path) == []


# ---------- 真跑 mvn 的部分 ----------

def test_red_suite_still_exits_zero_and_writes_reports(full_run):
    """-Dmaven.test.failure.ignore=true：测试一红 mvn 就非 0 退出，而报告已经
    写出来了 —— 不加这个标志，调用方会把退出码当成「没跑成」。"""
    assert full_run["proc"].returncode == 0, full_run["proc"].stdout[-2000:]
    assert _failures(full_run), "红了却没有失败被解析出来"


def test_report_paths_lists_every_test_class(full_run):
    """surefire 每个测试类一份报告 —— 只取一份会漏掉整个类的结果。"""
    names = [p.name for p in MavenAdapter().report_paths(full_run["repo"])]
    assert names == ["TEST-demo.CalcTest.xml", "TEST-demo.OtherTest.xml"], names


def test_parses_a_real_surefire_report(full_run):
    a = MavenAdapter()
    paths = a.report_paths(full_run["repo"])
    assert paths, "surefire 没产出报告"
    fs = parse_junit(paths, a.make_test_id)
    assert "demo.CalcTest#addWorks" in fs.ids
    assert "demo.CalcTest#divideBlowsUp" in fs.ids      # <error>，不是 <failure>
    assert "demo.CalcTest#alsoPasses" in fs.ran
    assert "demo.CalcTest#alsoPasses" not in fs.ids
    assert "demo.OtherTest#alwaysPasses" in fs.ran


def test_surefire_testcase_carries_no_file_attribute(full_run):
    """哨兵：pytest 侧的 make_test_id 靠 <testcase file=...>，surefire 不写。

    这条一旦变红说明 surefire 开始写 file 了，那时 Failure.file/line 才有值，
    locate_source 也就多了一条线索。现在它俩恒为 None。
    """
    root = ET.parse(
        MavenAdapter().report_paths(full_run["repo"])[0]).getroot()
    assert root.tag == "testsuite"          # pytest 侧是 testsuites
    cases = list(root.iter("testcase"))
    assert cases
    assert all(c.get("file") is None for c in cases), \
        [dict(c.attrib) for c in cases]


def test_scoped_id_is_runnable(scoped_run):
    """合成的 id 必须能被 -Dtest= 真正跑起来。

    对不上的 id 不会报错而是静默：surefire 一个用例都不跑，报告要么没有、
    要么 tests="0"，看起来像「复跑全过了」。
    """
    a = MavenAdapter()
    paths = a.report_paths(scoped_run["repo"], scoped=True)
    assert paths, scoped_run["proc"].stdout[-2000:]
    root = ET.parse(paths[0]).getroot()
    assert root.get("tests") == "1", dict(root.attrib)
    fs = parse_junit(paths, a.make_test_id)
    assert fs.ids == {"demo.CalcTest#addWorks"}, sorted(fs.ids)


def test_selector_for_a_whole_class_really_runs_it(class_selector_run):
    """`-Dtest=demo.CalcTest`（不带 `#方法`）真的跑起那个类，且只跑那个类。

    test_selectors 把改动过的测试文件翻成裸类名，整条挖掘链路的阶段 1/2 都
    靠它。这个语法只在文档里读到不算数：对不上的选择器不会报错，surefire
    安静地一个用例都不跑，报告里 tests="0"，上层读成「复跑全过了」。

    两个方向都要有：CalcTest 的三个用例都跑到（不是 0），OtherTest 一个都
    没跑到（不是退化成全量 —— 已实测 `-Dtest=demo.CalcTest#` 这种带空方法名
    的写法会被 surefire 当成没有过滤条件，把整个套件跑一遍）。
    """
    a = MavenAdapter()
    paths = a.report_paths(class_selector_run["repo"], scoped=True)
    assert [p.name for p in paths] == ["TEST-demo.CalcTest.xml"], \
        [p.name for p in paths] + [class_selector_run["proc"].stdout[-2000:]]
    fs = parse_junit(paths, a.make_test_id)
    assert fs.ran == {"demo.CalcTest#addWorks", "demo.CalcTest#divideBlowsUp",
                      "demo.CalcTest#alsoPasses"}, sorted(fs.ran)
    assert "demo.OtherTest#alwaysPasses" not in fs.ran, sorted(fs.ran)


def test_scoped_run_does_not_see_the_previous_runs_reports(stale_run):
    """陈旧报告：mvn test 不清空 target/surefire-reports/，而 report_paths
    只看文件系统当前状态。上一轮的失败会被当成这一轮的 —— flaky 复跑据此
    判定，不报错，只是判错。
    """
    # 前提：复跑之前上一轮的报告确实躺在目录里，否则这条测试什么都没验证
    assert stale_run["before"] == [
        "TEST-demo.CalcTest.xml", "TEST-demo.OtherTest.xml"], stale_run["before"]
    a = MavenAdapter()
    paths = a.report_paths(stale_run["repo"], scoped=True)
    assert [p.name for p in paths] == ["TEST-demo.OtherTest.xml"], \
        [p.name for p in paths]
    fs = parse_junit(paths, a.make_test_id)
    assert "demo.CalcTest#addWorks" not in fs.ids, "上一轮的失败被当成了这一轮的"
    assert fs.ran == {"demo.OtherTest#alwaysPasses"}, sorted(fs.ran)


def test_locate_source_filters_out_the_assertion_framework(full_run):
    """栈顶清一色是 JUnit 自己的帧 —— 只断言「抽出了帧」的话，一个只返回
    栈顶的实现也能过，而栈顶恰恰是 AssertionFailureBuilder.build。
    """
    fail = _failures(full_run)["demo.CalcTest#addWorks"]
    # 前提：这条 trace 里确实有断言框架的帧
    assert "org.junit.jupiter.api.AssertionFailureBuilder.build" in fail.trace
    assert "java.base/java.lang.reflect.Method.invoke" in fail.trace
    cands = MavenAdapter().locate_source(fail, full_run["repo"])
    assert all(not c.frame.startswith(
        ("org.junit", "org.opentest4j", "java.", "jdk.", "sun."))
        for c in cands), [c.frame for c in cands]


def test_locate_source_invents_no_path_for_a_test_only_class(full_run):
    """断言失败的栈里唯一的项目内帧是测试类，而它在 src/test/java。

    映射不出真实存在的文件时宁可一个候选都不给：SourceCandidate.path 会进
    模型的提示词，一个不存在的路径会让模型去读空文件、甚至凭空造改动。
    """
    fail = _failures(full_run)["demo.CalcTest#addWorks"]
    assert "demo.CalcTest.addWorks(CalcTest.java:" in fail.trace
    assert MavenAdapter().locate_source(fail, full_run["repo"]) == []


def test_locate_source_puts_the_deepest_main_source_frame_first(full_run):
    """Java 堆栈由深入浅打印（栈顶就是抛出点），与 Python traceback 相反。"""
    fail = _failures(full_run)["demo.CalcTest#divideBlowsUp"]
    repo = full_run["repo"]
    cands = MavenAdapter().locate_source(fail, repo)
    # 测试类的帧、java.base 的帧全都不该在里面；剩下两条产品代码的帧里，
    # 真正抛异常的 doDivide 必须排在调用它的 divide 前面
    assert [c.frame for c in cands] == [
        "demo.Calc.doDivide", "demo.Calc.divide"], \
        [(c.path, c.frame) for c in cands]
    assert all(c.path == "src/main/java/demo/Calc.java" for c in cands)
    src = (repo / cands[0].path).read_text(encoding="utf-8").splitlines()
    # 行号钉到真正抛异常的那一行，而不是钉死一个数字
    assert "a / b" in src[cands[0].line - 1], src[cands[0].line - 1]
