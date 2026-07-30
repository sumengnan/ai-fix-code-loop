"""`aifix mine` 在一个真实 Maven 仓库上真的挖得出任务。

这是第四、第五处适配层裂缝的最终判据。单测只能证明 test_selectors 与
is_file_level_id 各自自洽；只有让 mine_tasks 在一个真的有「红转绿」提交的
Maven git 仓库上跑完四个阶段，才知道挖掘链路上还有没有别处写死了 pytest
的假设 —— 此前它对任何 Maven 工程恒定产出 0 个任务，且不报一个错。

**「产出了 N 条记录」证明不了任何事**：最后一条把挖出来的任务
prepare_task_repo 到临时目录，真跑一次全量，确认 target_test 确实在失败集
里。任务集是要被反复使用的 ground truth，一条复现不了的任务会让所有模型的
成功率一起变低，看起来像「模型都不行」。

mvn 慢（每次约 3 秒，这个文件一共起 5 次），所以整跑一次的夹具是 module 级。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from aifix.adapters.maven_adapter import MavenAdapter
from aifix.eval.mine import mine_tasks
from aifix.eval.workspace import prepare_task_repo
from aifix.nodes.baseline import run_full_suite

from tests.conftest import maven_skip_mark  # noqa: E402

# 判据是「mvn -o 跑不跑得起来」，不是「mvn 在不在」——
# 见 conftest.maven_offline_reason 里那段实测。
pytestmark = maven_skip_mark()

# 与 tests/test_maven_adapter.py 同一份 pom：surefire 与 junit-jupiter 都钉死
# 版本（Maven 3.9 默认绑的 surefire 2.12.4 跑不了 JUnit 5，不钉版本会静默
# 一个用例都不跑），两个版本本机 ~/.m2 里都有，配合 -o 可以完全离线。
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

_CALC_BUGGY = """package demo;

public class Calc {
    public int add(int a, int b) {
        return a - b;   // bug: 应为 a + b
    }
}
"""

_CALC_FIXED = """package demo;

public class Calc {
    public int add(int a, int b) {
        return a + b;
    }
}
"""

# C^ 的测试：只有 zeroIsStable，在 bug 下也通过（0-0 与 0+0 都是 0）。
# 它的作用是让「阶段 1 真的跑到了两个用例」与「阶段 1 只跑到一个」能分开：
# 一份只含红用例的测试类没法证明 scoped 范围是对的。
_TEST_BEFORE = """package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void zeroIsStable() {
        assertEquals(0, new Calc().add(0, 0));
    }
}
"""

_TEST_AFTER = """package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void zeroIsStable() {
        assertEquals(0, new Calc().add(0, 0));
    }

    @Test
    void addWorks() {
        assertEquals(3, new Calc().add(1, 2));
    }
}
"""

_SRC_PATH = "src/main/java/demo/Calc.java"
_TEST_PATH = "src/test/java/demo/CalcTest.java"
_TARGET = "demo.CalcTest#addWorks"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _write_history(root: Path) -> dict:
    """两个提交的 Maven 仓库：C^ 的 add 是错的，C 把它改对并补上用例。

    C 同时动了 src/main/java 与 src/test/java —— is_candidate 要求两侧都动，
    只动一侧的提交连候选都不是。
    """
    (root / "src/main/java/demo").mkdir(parents=True)
    (root / "src/test/java/demo").mkdir(parents=True)
    (root / "pom.xml").write_text(_POM, encoding="utf-8")
    (root / _SRC_PATH).write_text(_CALC_BUGGY, encoding="utf-8")
    (root / _TEST_PATH).write_text(_TEST_BEFORE, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init：add 写成了减法")
    base = _git(root, "rev-parse", "HEAD").strip()

    (root / _SRC_PATH).write_text(_CALC_FIXED, encoding="utf-8")
    (root / _TEST_PATH).write_text(_TEST_AFTER, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fix: add 应为加法，补上 addWorks")
    return {"path": root, "base": base,
            "commit": _git(root, "rev-parse", "HEAD").strip()}


@pytest.fixture(scope="module")
def mined(tmp_path_factory) -> dict:
    """跑一次 mine_tasks。四个阶段各起一次 mvn，整个文件只跑这一次。"""
    root = tmp_path_factory.mktemp("mvn_mine")
    hist = _write_history(root / "proj")
    seen: list[tuple] = []
    tasks = asyncio.run(mine_tasks(
        str(hist["path"]), MavenAdapter(), limit=10,
        workdir=tmp_path_factory.mktemp("mine_work"),
        on_progress=lambda sha, n, error=None: seen.append((sha, n, error))))
    return {"hist": hist, "tasks": tasks, "progress": seen}


@pytest.fixture(scope="module")
def replayed(mined, tmp_path_factory) -> dict:
    """把挖出来的任务还原成仓库，真跑一次全量 —— 不跑就什么都没证明。"""
    if not mined["tasks"]:
        pytest.skip("没挖出任务，复现无从谈起（由 test_mining_really_produces_tasks 报告）")
    dest = tmp_path_factory.mktemp("mvn_replay") / "task"
    prepare_task_repo(mined["tasks"][0], dest)
    fs = asyncio.run(run_full_suite(dest, MavenAdapter(), require_report=True))
    return {"dest": dest, "fs": fs}


def test_mining_really_produces_tasks(mined):
    """整件事的目的：此前对任何 Maven 工程恒定 0 个任务，且不报一个错。

    同时盯 on_progress：n≥0 是「这个 commit 产出了 n 个可用用例」，n=-1 是
    「验证跑挂了被跳过」。两者必须能分开 —— 修复之前看到的正是 n=0，与
    「这个仓库最近没有红转绿的提交」这个正常结果完全一样。
    """
    assert len(mined["tasks"]) > 0, mined["progress"]
    # 只有 C 会被验证：根提交没有父提交，直接跳过
    assert mined["progress"] == [(mined["hist"]["commit"], 1, None)], \
        mined["progress"]


def test_the_mined_target_is_a_surefire_selector(mined):
    """target_test 的形状是 `全限定类名#方法名`，不是文件路径。

    它会被原样喂回 `-Dtest=`（评测时 run_task 复现目标用例就靠它）。喂路径
    进去不报错，surefire 安静地一个用例都不跑。
    """
    t = mined["tasks"][0]
    assert t.target_test == _TARGET, t.target_test
    assert t.adapter == "maven", t.adapter
    assert t.test_files == [_TEST_PATH], t.test_files
    assert t.commit == mined["hist"]["commit"]
    assert t.base_commit == mined["hist"]["base"]


def test_gold_files_are_the_java_sources_only(mined):
    """gold_files 是 src/main/java 下的 `.java` —— locate_hit 的判定依据。

    pom.xml 也在这个 commit 的改动之外，但即便它变了也不该进来：它不是
    locate_source 能指向的东西，掺进去等于给 Detector 记一个它按设计就
    拿不到的分。
    """
    gold = mined["tasks"][0].gold_files
    assert gold == [_SRC_PATH], gold
    assert all(g.startswith("src/main/java/") and g.endswith(".java")
               for g in gold), gold


def test_the_mined_task_really_reproduces_the_failure(mined, replayed):
    """把任务还原成仓库、真跑一次全量，target_test 必须在失败集里。

    「产出了 N 条记录」证明不了任何事。一条复现不了的任务不会报错，只会让
    所有模型在它上面恒判 SAME，看起来像模型不行。

    两个方向都要有：目标用例红（不是绿、也不是压根没跑到），另一个用例绿
    （不是「整个套件都红」——那种仓库拿任何 target 都能过上半条断言）。
    """
    fs = replayed["fs"]
    assert _TARGET in fs.ran, sorted(fs.ran)
    assert _TARGET in fs.ids, sorted(fs.ids)
    assert "demo.CalcTest#zeroIsStable" in fs.ran, sorted(fs.ran)
    assert "demo.CalcTest#zeroIsStable" not in fs.ids, sorted(fs.ids)


def test_the_replayed_repo_is_the_base_source_with_the_new_test(replayed, mined):
    """还原出来的树是「C^ 的源码 + C 的测试」这个人造状态。

    没有这一条，上面那条「目标用例红」可能只是因为 prepare_task_repo 压根
    没换源码（C^ 与 C 的源码相同时它也是红的吗 —— 不是，但那时红的原因
    是别的），这条把状态本身钉死。
    """
    dest = replayed["dest"]
    assert (dest / _SRC_PATH).read_text(encoding="utf-8") == _CALC_BUGGY
    assert (dest / _TEST_PATH).read_text(encoding="utf-8") == _TEST_AFTER
