"""共享夹具：一个可复现的、带失败测试的临时 git 仓库。"""
from __future__ import annotations

import functools
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import pytest


# Maven 那三个测试文件的跳过判据。
#
# **不能只查 `mvn` 在不在**——那是实测栽过的（2026-07-30，issue #2 的真跑）：
# GitHub 的 ubuntu 镜像自带 Maven，于是判据成立、测试照跑，而 runner 的 `~/.m2`
# 是空的、适配器命令里带 `-o`（离线），19 个用例全红。它们出现在 aifix 的
# baseline 里，被当成待修的 bug 排进队列。
#
# 真正的前提是「**`mvn -o` 能把这几个钉死版本的构件解析出来**」，所以判据就去
# 探这件事。代价是有 mvn 的机器上每个会话多花约 8 秒，换掉一整类「不跳过、
# 真失败」的假红。
#
# 版本与三个测试文件里的 pom 保持一致。真出现漂移时的表现是**那个文件的测试
# 不跳过而是失败**——与修这条之前一样，没有变得更糟。
_PROBE_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>probe</groupId><artifactId>probe</artifactId><version>1.0</version>
  <properties><maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies><dependency>
    <groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
    <version>5.10.2</version><scope>test</scope></dependency></dependencies>
  <build><plugins>
    <plugin><groupId>org.apache.maven.plugins</groupId>
      <artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>
    <plugin><groupId>org.apache.maven.plugins</groupId>
      <artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
  </plugins></build>
</project>
"""

_PROBE_TEST = """package p;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertTrue;
class ProbeTest { @Test void ok() { assertTrue(true); } }
"""


@functools.lru_cache(maxsize=1)
def maven_offline_reason() -> str | None:
    """`mvn -o` 跑得起来就返回 None，否则返回跳过的理由。整个会话只探一次。"""
    if shutil.which("mvn") is None:
        return "本机没有 mvn"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src" / "test" / "java" / "p").mkdir(parents=True)
        (root / "pom.xml").write_text(_PROBE_POM, encoding="utf-8")
        (root / "src" / "test" / "java" / "p" / "ProbeTest.java").write_text(
            _PROBE_TEST, encoding="utf-8")
        res = subprocess.run(["mvn", "-B", "-q", "-o", "test"], cwd=root,
                             capture_output=True, text=True)
    if res.returncode != 0:
        return ("本机 ~/.m2 里缺 junit-jupiter 5.10.2 / surefire 3.2.5，"
                "`mvn -o` 离线跑不起来")
    return None


def maven_skip_mark():
    reason = maven_offline_reason()
    return pytest.mark.skipif(reason is not None,
                              reason=f"跳过 Maven 测试：{reason}")


@pytest.fixture
def real_venv():
    """造一个**真的** venv（自己的 prefix、自己的 bin/python）的工厂。

    用一条 `.pth` 把当前解释器的 site-packages 接过去，于是它看得见 pytest，
    整个过程不联网。

    真 venv 而不是假脚本：假脚本能骗过每一条只看命令字符串的断言，却证明不了
    「换了解释器之后测试真的还跑得起来」——而这正是这一整条解释器链路存在的
    唯一理由。放在 conftest 是因为解释器解析有两条独立的入口（核心循环、
    评测的 run_task），两边都要用它来做端到端断言。
    """
    def make(path: Path) -> Path:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(path)],
            check=True, capture_output=True)
        sp = next(iter(sorted((path / "lib").glob("python*")))) / "site-packages"
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "_aifix_link.pth").write_text(
            sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
        exe = path / "bin" / "python"
        assert exe.is_file() and str(exe) != sys.executable
        return exe
    return make

_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b
'''

_FIXED = '''def add(a, b):
    return a + b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_identity():
    assert add(0, 0) == 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True).stdout


@pytest.fixture
def buggy_repo(tmp_path: Path) -> Path:
    """一个 pytest 项目：test_add 失败，test_identity 通过。"""
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def fixed_source() -> str:
    return _FIXED


_TEST_ONLY_IDENTITY = '''from calc import add


def test_identity():
    assert add(0, 0) == 0
'''

_TEST_BOTH = '''from calc import add


def test_identity():
    assert add(0, 0) == 0


def test_add():
    assert add(2, 3) == 5
'''


@pytest.fixture
def history_repo(tmp_path: Path) -> dict:
    """一个带「红转绿」commit 的仓库，供挖掘与评测使用。

    C^ : calc.py 有 bug，测试里只有 test_identity（通过）
    C  : calc.py 修好，测试里多出 test_add

    于是「C^ 的源码 + C 的测试」= test_add 红，而 C 处全绿 ——
    正是 aifix mine 要找的形状。
    """
    repo = tmp_path / "hist"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        _TEST_ONLY_IDENTITY, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "calc.py").write_text(_FIXED, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST_BOTH, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: add 应为加法")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    return {"path": repo, "base": base, "commit": commit,
            "test_files": ["tests/test_calc.py"], "gold_files": ["calc.py"],
            "target": "tests/test_calc.py::test_add"}
