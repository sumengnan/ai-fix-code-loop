"""Maven 端到端验收：红色 Java 测试进去，绿色分支出来，主工作区未被触碰。

这是适配层抽象成不成立的最终判据。单测只能证明 MavenAdapter 自己自洽，
只有让一个真的 Maven 工程走完 preflight → baseline → detect → fix →
verify → report 的全程，才知道核心循环里还有没有别处写死了 pytest 的假设。

用脚本化模型替身，不打网络；mvn 用 -o 离线。mvn 每次几秒，所以整跑一次的
夹具是 module 级，所有断言共用那一次的产物。不需要 mvn 的几条（守卫、
split_paths）不挂 skip，本机没有 Java 工具链时它们照样有价值。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.llm.base import StreamChunk, ToolCallDelta
from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolError
from harness.usage import Usage

from aifix.signals import under_dirs
from aifix.adapters.maven_adapter import MavenAdapter
from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.eval.mine import split_paths
from aifix.tools.patch import ApplyPatchTool


def _dirs(dirs):
    """目录列表 → `ProjectAdapter.is_test_path` 那种谓词。

    守卫从「收目录列表」改成「收谓词」（为了 vitest 的同目录布局）之后，
    这些用例各自在考的判断没有变。**逐个包、不统一换成
    `PytestAdapter().is_test_path`**：那会把只给 `["tests"]` 的用例悄悄放宽
    成 `["tests", "test"]`，考的东西被改掉了而测试照样绿。
    """
    return lambda p: under_dirs(p, dirs)


from tests.conftest import maven_skip_mark  # noqa: E402

# 判据是「mvn -o 跑不跑得起来」，不是「mvn 在不在」——
# 见 conftest.maven_offline_reason 里那段实测。
needs_mvn = maven_skip_mark()

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

_CALC = """package demo;

public class Calc {
    public int add(int a, int b) {
        return a - b;   // bug: 应为 a + b
    }
}
"""

# zeroIsStable 在修复前后都通过（0-0 与 0+0 都是 0）。它的作用是给判定制造
# 区分度：它必须始终不在 baseline_ids 里，也必须不因补丁而转红 —— 否则
# verify 会判 WORSE。用 add(1,1)==0 那种「靠 bug 才通过」的用例会让整条
# 端到端在补丁正确时反而失败。
_CALC_TEST = """package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;


class CalcTest {
    @Test
    void addWorks() {
        assertEquals(3, new Calc().add(1, 2));
    }

    @Test
    void zeroIsStable() {
        assertEquals(0, new Calc().add(0, 0));
    }
}
"""

_SRC_PATH = "src/main/java/demo/Calc.java"
_TEST_PATH = "src/test/java/demo/CalcTest.java"
_TARGET_ID = "demo.CalcTest#addWorks"

# 空的上下文行写成一个空格：diff 的上下文行以空格开头，源文件里的空行
# 对应的就是一行单独的空格。写成完全空行多数 git 也认，但那是宽容而非规范。
_GOOD_PATCH = "\n".join([
    "--- a/src/main/java/demo/Calc.java",
    "+++ b/src/main/java/demo/Calc.java",
    "@@ -1,7 +1,7 @@",
    " package demo;",
    " ",
    " public class Calc {",
    "     public int add(int a, int b) {",
    "-        return a - b;   // bug: 应为 a + b",
    "+        return a + b;",
    "     }",
    " }",
    ""])

# 「把断言改成迁就 bug」——这个 diff 本身完全打得上（下面有一条对照测试
# 用 pytest 布局的 test_dirs 证明它真的能应用），拦住它的只能是守卫。
_TEST_PATCH = "\n".join([
    "--- a/src/test/java/demo/CalcTest.java",
    "+++ b/src/test/java/demo/CalcTest.java",
    "@@ -7,7 +7,7 @@",
    " class CalcTest {",
    "     @Test",
    "     void addWorks() {",
    "-        assertEquals(3, new Calc().add(1, 2));",
    "+        assertEquals(-1, new Calc().add(1, 2));",
    "     }",
    " ",
    "     @Test",
    ""])

# pytest 布局的测试目录，供对照测试使用
_PYTEST_DIRS = ["tests", "test"]

_DIAG = json.dumps({
    "suspect_file": _SRC_PATH, "suspect_lines": [4, 6],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _write_project(root: Path) -> Path:
    """Maven 标准布局 + git 仓库。核心循环要求主工作区是 git 仓库。"""
    (root / "src/main/java/demo").mkdir(parents=True)
    (root / "src/test/java/demo").mkdir(parents=True)
    (root / "pom.xml").write_text(_POM, encoding="utf-8")
    (root / _SRC_PATH).write_text(_CALC, encoding="utf-8")
    (root / _TEST_PATH).write_text(_CALC_TEST, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(scope="module")
def maven_run(tmp_path_factory) -> dict:
    """跑一次完整的 aifix run。整个文件只起这一次 mvn 序列。

    Fixer 的剧本刻意先打一个改测试文件的补丁：那是模型最常见的作弊路径，
    也让「不许改测试文件」守卫在真实的多段测试目录（src/test/java/...）上
    被真的触发一次，而不是只在单测里被构造出来。
    """
    repo = _write_project(tmp_path_factory.mktemp("mvn_e2e") / "proj")
    before = {
        "calc": (repo / _SRC_PATH).read_text(encoding="utf-8"),
        "test": (repo / _TEST_PATH).read_text(encoding="utf-8"),
        "head": _git(repo, "rev-parse", "HEAD").strip(),
    }
    state = asyncio.run(run_once(
        repo, AifixConfig(), run_id="mvne2e",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _TEST_PATCH})),
            _tool("apply_patch", json.dumps({"diff": _GOOD_PATCH})),
            _text("已修复"),
        ])))
    return {"repo": repo, "state": state, "before": before}


@needs_mvn
def test_red_java_in_green_branch_out(maven_run):
    """整条链路跑通：surefire 的 id 一路流到判定与报告。"""
    state = maven_run["state"]
    assert state["abort"] is None, state["abort"]
    assert state["adapter_name"] == "maven"
    # baseline 只该有那一个红的；zeroIsStable 在修复前后都通过
    assert state["baseline_ids"] == [_TARGET_ID], state["baseline_ids"]
    assert [r["verdict"] for r in state["results"]] == ["better"], state["results"]
    assert "1 / 1" in state["report_md"]


@needs_mvn
def test_the_fix_is_really_on_the_delivery_branch(maven_run):
    """判定说「已修复」，交付分支上就必须真有那个提交。"""
    repo = maven_run["repo"]
    branch = maven_run["state"]["branch"]
    assert branch == "aifix/mvne2e"
    src = _git(repo, "show", f"{branch}:{_SRC_PATH}")
    assert "return a + b;" in src, src
    # 分支比主分支恰好多一个提交，且那个提交只动了产品代码
    assert _git(repo, "rev-list", "--count",
                f"{maven_run['before']['head']}..{branch}").strip() == "1"
    changed = _git(repo, "show", "--name-only", "--pretty=format:",
                   branch).split()
    assert changed == [_SRC_PATH], changed


@needs_mvn
def test_main_worktree_is_untouched(maven_run):
    """主工作区一字未动 —— 隔离是这个工具敢自动改代码的全部前提。"""
    repo, before = maven_run["repo"], maven_run["before"]
    assert (repo / _SRC_PATH).read_text(encoding="utf-8") == before["calc"]
    assert (repo / _TEST_PATH).read_text(encoding="utf-8") == before["test"]
    assert _git(repo, "rev-parse", "HEAD").strip() == before["head"]
    assert _git(repo, "status", "--porcelain",
                "--untracked-files=no").strip() == ""


@needs_mvn
def test_maven_build_output_never_reaches_the_delivery_branch(maven_run):
    """交付分支的树 = 初始的四个文件，一个构建产物都没有。

    非空转：baseline 解析出 `demo.CalcTest#addWorks` 的唯一来源是
    target/surefire-reports/TEST-*.xml，而 verify 提交时 mvn 刚跑完
    （clean test），target/ 里的 classes / test-classes / surefire-reports
    正躺在 worktree 里且全都未跟踪。它们没进分支，是因为 Worktree.commit
    只 `git add -- <ApplyPatchTool 记账的路径>`。

    这一条同时否掉一句在多处注释里流传的说法：「Worktree.commit() 的
    git add -A 会把产物扫进交付分支」—— commit 里没有 git add -A，
    它的 docstring 甚至专门写着绝不用。
    """
    # 「target/ 真的存在过」的凭据留在这里：这个 id 只可能解析自
    # target/surefire-reports/TEST-demo.CalcTest.xml
    assert maven_run["state"]["baseline_ids"] == [_TARGET_ID]
    tree = _git(maven_run["repo"], "ls-tree", "-r", "--name-only",
                maven_run["state"]["branch"]).split()
    assert sorted(tree) == sorted(
        ["pom.xml", _SRC_PATH, _TEST_PATH]), tree


@needs_mvn
def test_the_no_test_edits_guard_fired_during_the_real_run(maven_run):
    """守卫在真实 run 里拦下了 src/test/java/... 的补丁。

    证据取自 trace 的领域事实：count_violations 按 apply_patch 的错误内容
    归类，`test_edit` 这条只可能来自 tools/patch.py 的那句「拒绝修改测试
    文件」。断言测试文件没被改动是不够的 —— 补丁根本没打上去（diff 写错、
    工具没被调用）时那条断言同样成立。
    """
    facts = (Path(maven_run["state"]["artifact_dir"]) / "facts.jsonl")
    rows = [json.loads(ln) for ln in
            facts.read_text(encoding="utf-8").splitlines() if ln.strip()]
    violations = [r["value"] for r in rows if r["key"] == "violation"]
    assert "test_edit" in violations, rows
    # 而且守卫拦住之后这一轮没有白跑：真正的补丁仍然打上了
    assert [r["verdict"] for r in maven_run["state"]["results"]] == ["better"]


# ---------- 不需要 mvn：守卫与路径拆分在 Maven 布局下的判定 ----------

@pytest.fixture
def project(tmp_path) -> Path:
    return _write_project(tmp_path / "proj")


async def test_patch_guard_refuses_the_maven_test_file(project):
    """`src/test/java/demo/CalcTest.java` 必须被拦住。

    这是 M4 那次「按路径分段比前缀」改动的真实消费者：MavenAdapter 的
    test_dirs 是 `["src/test"]`，首段是 `src`。改动之前的守卫写的是
    `parts[0] in test_dirs`，这个补丁会被直接放行。
    """
    sb = LocalSandbox(workspace=str(project))
    await sb.start()
    try:
        tool = ApplyPatchTool(sb, is_test=MavenAdapter().is_test_path)
        with pytest.raises(ToolError, match="拒绝修改测试文件"):
            await tool.run(tool.Params(diff=_TEST_PATCH))
        assert (project / _TEST_PATH).read_text(encoding="utf-8") == _CALC_TEST
    finally:
        await sb.close()


async def test_that_same_patch_applies_cleanly_without_the_guard(project):
    """对照：这个 diff 本身完全打得上，拦住它的只能是守卫。

    没有这一条，上面那条测试无法区分「守卫拦住了」和「diff 本来就是废的」。
    换成 pytest 布局的 test_dirs（`["tests", "test"]`），同一个补丁应用成功。
    """
    sb = LocalSandbox(workspace=str(project))
    await sb.start()
    try:
        tool = ApplyPatchTool(sb, is_test=_dirs(_PYTEST_DIRS))
        out = await tool.run(tool.Params(diff=_TEST_PATCH))
        assert "补丁已应用" in out
        assert "assertEquals(-1" in (project / _TEST_PATH).read_text(
            encoding="utf-8")
    finally:
        await sb.close()


async def test_guard_still_lets_the_production_source_through(project):
    """反向断言：产品代码必须放行 —— 一个「什么都拦」的守卫也能过上面两条。"""
    sb = LocalSandbox(workspace=str(project))
    await sb.start()
    try:
        touched: set[str] = set()
        tool = ApplyPatchTool(sb, is_test=MavenAdapter().is_test_path,
                              touched=touched)
        await tool.run(tool.Params(diff=_GOOD_PATCH))
        assert "return a + b;" in (project / _SRC_PATH).read_text(
            encoding="utf-8")
        # 记账的路径就是交付时 git add 的路径
        assert touched == {_SRC_PATH}
    finally:
        await sb.close()


def test_split_paths_classifies_the_maven_test_dir(project):
    """挖任务侧对 `src/test/java/...` 的判定 —— 与守卫共用同一个谓词。

    路径取自真实存在的文件：写死一个不存在的路径，这条测试就退化成
    在验证一个字符串函数对虚构输入的行为。
    """
    paths = [_TEST_PATH, _SRC_PATH, "pom.xml"]
    for p in paths:
        assert (project / p).is_file(), p
    adapter = MavenAdapter()
    tests, gold = split_paths(paths, adapter.is_test_path,
                              adapter.source_suffixes())
    assert tests == [_TEST_PATH], tests
    # 这里曾是一条「现状快照」：源文件侧写死 `.py`，Java 源码一律落空，
    # gold 恒空 → is_candidate 恒 False → aifix mine 对 Maven 工程一个任务
    # 都挖不出来。缺口已补（后缀改由 adapter.source_suffixes() 回答），
    # 快照随之转正。
    assert gold == [_SRC_PATH], gold
