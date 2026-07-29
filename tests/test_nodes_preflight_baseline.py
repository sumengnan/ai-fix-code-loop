import subprocess
import sys
import typing
from pathlib import Path

import pytest

from aifix.adapters.base import ProjectAdapter
from aifix.adapters.maven_adapter import MavenAdapter
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.delivery import Worktree
from aifix.graph import AifixState, new_state
from aifix.nodes.baseline import (adapter_for, baseline_node, run_full_suite,
                                  run_scoped)
from aifix.nodes.preflight import preflight_node


def _init_git(repo: Path) -> None:
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, text=True)


def _maven_project(repo: Path, *, also_python: bool = False) -> Path:
    """Maven 标准布局的最小工程。preflight 只看 detect()，不跑 mvn。"""
    (repo / "src/main/java/demo").mkdir(parents=True)
    (repo / "src/test/java/demo").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n",
        encoding="utf-8")
    (repo / "src/main/java/demo/Calc.java").write_text(
        "package demo;\npublic class Calc {}\n", encoding="utf-8")
    (repo / "src/test/java/demo/CalcTest.java").write_text(
        "package demo;\nclass CalcTest {}\n", encoding="utf-8")
    if also_python:
        # Java 工程的工具链里带 Python 脚本是常事（发版、代码生成、CI 胶水），
        # 而 PytestAdapter.detect 只要看见 pyproject.toml 或 tests/ 就认领。
        (repo / "tests").mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='t'\n",
                                             encoding="utf-8")
    _init_git(repo)
    return repo


def test_new_state_defaults(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    assert st["run_id"] == "r1"
    assert st["queue"] == []
    assert st["current"] is None
    assert st["attempt"] == 0
    assert st["results"] == []


def test_preflight_detects_adapter_and_rejects_dirty(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["adapter_name"] == "pytest"
    assert out["abort"] is None

    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    st2 = new_state(buggy_repo, AifixConfig(), run_id="r2")
    out2 = preflight_node(st2)
    assert out2["abort"] is not None
    assert "工作区不干净" in out2["abort"]


def test_preflight_detects_a_maven_project(tmp_path):
    """新适配器加进注册表却接不上探测，等于没加：Maven 工程会直接 abort。

    preflight 一度有第二份注册表（`ADAPTERS = [PytestAdapter]`），
    adapter_name 由它决定 —— baseline 那边把 maven 登记好了也没用。
    """
    repo = _maven_project(tmp_path / "mvn")
    out = preflight_node(new_state(repo, AifixConfig(), run_id="r1"))
    assert out["abort"] is None, out["abort"]
    assert out["adapter_name"] == "maven"


def test_preflight_still_detects_a_pytest_project(buggy_repo):
    """反向断言：一个「无脑返回 maven」的实现必须过不了这里。"""
    out = preflight_node(new_state(buggy_repo, AifixConfig(), run_id="r1"))
    assert out["adapter_name"] == "pytest"


def test_maven_wins_over_pytest_when_both_detect(tmp_path):
    """两个 detect() 同时命中时的顺序是显式决定，不是注册表的书写巧合。

    PytestAdapter.detect 极宽松（pyproject.toml 或 tests/ 存在即认领），
    MavenAdapter.detect 要求根目录有 pom.xml —— 具体的那个先问。
    反过来的话，任何带一个 Python 脚本目录的 Java 工程都会被当成 pytest
    工程：随后 baseline 跑的是 pytest 命令，一个用例都收不到，报告写
    「0 个失败」，看起来一切正常。
    """
    repo = _maven_project(tmp_path / "poly", also_python=True)
    # 前提：这个仓库确实两个 detect 都命中，否则这条测试什么都没验证
    assert MavenAdapter.detect(repo) is True
    assert PytestAdapter.detect(repo) is True
    out = preflight_node(new_state(repo, AifixConfig(), run_id="r1"))
    assert out["adapter_name"] == "maven"


@pytest.mark.parametrize("kind", ["maven", "pytest"])
def test_detected_name_is_resolvable_by_adapter_for(tmp_path, buggy_repo, kind):
    """探测出的名字必须能被 adapter_for 取到 —— 两份注册表就是从这里裂的。

    preflight 按 detect 选、baseline 按名字取，各存一份的话新增适配器时
    只改一处不会有任何报错：探测得到一个名字，取的时候 KeyError，或者反
    过来登记了却永远探测不到。
    """
    repo = buggy_repo if kind == "pytest" else _maven_project(tmp_path / "m")
    name = preflight_node(new_state(repo, AifixConfig(), run_id="r1"))["adapter_name"]
    assert name == kind
    assert adapter_for(name).name == kind


_PROTOCOL_MEMBERS = sorted(n for n in vars(ProjectAdapter)
                           if not n.startswith("_") and n != "name")


@pytest.mark.parametrize("cls", [PytestAdapter, MavenAdapter])
def test_adapter_matches_the_protocol_member_for_member(cls):
    """注册表里的每个实现都要真的满足 ProjectAdapter，逐个成员比签名。

    Protocol 不做运行时检查：baseline 把参数标成 ProjectAdapter 之后，
    一个签名对不上的适配器照样能被登记、被 adapter_for 取出来，直到
    run_full_suite 调到那个方法才炸 —— 而那时已经跑完一次全量测试了。
    """
    import inspect
    # 防空转：成员列表是从协议对象上反射出来的，协议一旦改成别的写法
    # （比如成员只剩注解）这里会变成空列表，循环一次不跑而测试照样绿
    assert len(_PROTOCOL_MEMBERS) == 11, _PROTOCOL_MEMBERS
    assert isinstance(getattr(cls, "name", None), str) and cls.name
    for member in _PROTOCOL_MEMBERS:
        impl = getattr(cls, member, None)
        assert impl is not None, f"{cls.__name__} 缺少协议成员 {member}"
        assert str(inspect.signature(impl)) == \
            str(inspect.signature(getattr(ProjectAdapter, member))), member


@pytest.mark.parametrize("fn", [run_full_suite, run_scoped])
def test_suite_runners_are_annotated_with_the_protocol(fn):
    """`adapter: PytestAdapter` 在注册表有第二个实现之后就是一句假话。

    注解不会在运行时报错，所以这条只能盯注解本身：它写的是「这里只接
    PytestAdapter」，而 adapter_for 早已可能返回 MavenAdapter。读代码的人
    和静态检查都会据此得出错误结论。
    """
    hints = typing.get_type_hints(fn)
    assert hints["adapter"] is ProjectAdapter, hints["adapter"]


def test_preflight_rejects_unknown_project(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    st = new_state(tmp_path, AifixConfig(), run_id="r1")
    out = preflight_node(st)
    assert out["abort"] is not None
    assert "适配器" in out["abort"]


async def test_baseline_collects_failures(buggy_repo):
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert "tests/test_calc.py::test_add" in out["baseline_ids"]
    assert "tests/test_calc.py::test_identity" not in out["baseline_ids"]
    assert out["queue"] == ["tests/test_calc.py::test_add"]
    assert "tests/test_calc.py::test_add" in out["_failures"]


class _SilentAdapter(PytestAdapter):
    """跑一个什么都不做的命令：模拟测试进程被超时杀掉、根本没写出报告。"""

    def full_test_command(self) -> list[str]:
        return [sys.executable, "-c", ""]

    def scoped_test_command(self, test_ids) -> list[str]:
        return [sys.executable, "-c", ""]


async def test_missing_report_is_tolerated_by_default(buggy_repo):
    """M1/M2 的既有行为：报告缺失 → 空集合，不抛 —— 不能改。"""
    fs = await run_full_suite(buggy_repo, _SilentAdapter())
    assert fs.ids == set()
    fs2 = await run_scoped(buggy_repo, _SilentAdapter(), ["随便一个"])
    assert fs2.ids == set()


async def test_missing_report_raises_when_required(buggy_repo):
    """挖任务时「没跑成」必须与「跑完了、全绿」区分开。

    否则空集合会被当成全绿，`red - green` 把 base 处所有红的用例
    全部当成「红转绿」吐出来 —— 凭空捏造一整批任务。
    """
    with pytest.raises(RuntimeError, match="报告"):
        await run_full_suite(buggy_repo, _SilentAdapter(), require_report=True)
    with pytest.raises(RuntimeError, match="报告"):
        await run_scoped(buggy_repo, _SilentAdapter(), ["随便一个"],
                         require_report=True)


class _ZeroCaseAdapter(PytestAdapter):
    """收集阶段整轮中止：pytest 退 4，写出一份 `tests="0"` 的报告。

    命令逐字取自实测（pytest 9.1.1，2026-07-29）—— 给一个不存在的 node id
    就是这个形状：报告文件**存在**，里面一个 <testcase> 都没有。
    """

    def _cmd(self, report: str) -> list[str]:
        return [sys.executable, *self._BASE, f"--junitxml={report}",
                "tests/test_calc.py::压根不存在的用例"]

    def full_test_command(self) -> list[str]:
        return self._cmd(self.REPORT_NAME)

    def scoped_test_command(self, test_ids) -> list[str]:
        return self._cmd(self.SCOPED_REPORT_NAME)


async def test_a_report_with_zero_cases_is_not_all_green(buggy_repo):
    """报告存在但一个用例都没跑 —— 与「跑完了、全绿」必须分开。

    `require_report` 只查「有没有报告文件」，挡不住这一种：pytest 在收集阶段
    整轮中止（无效 node id、conftest 抛异常、依赖缺失）时退 4，**并写出一份
    `tests="0"` 的报告**。文件在，检查放行，parse_junit 解出空集合，于是
    baseline 读成「全绿」、verify 读成「补丁修好了一切」—— 正是 require_report
    这道闸要挡的事，只是换了个形状绕过去。

    docs/superpowers/specs/2026-07-28-m4-conclusive-design.md 第 62 行早就
    记着这个形状，一直没有对应的闸。
    """
    with pytest.raises(RuntimeError, match="一个用例都没跑"):
        await run_full_suite(buggy_repo, _ZeroCaseAdapter(),
                             require_report=True)
    with pytest.raises(RuntimeError, match="一个用例都没跑"):
        await run_scoped(buggy_repo, _ZeroCaseAdapter(), ["随便一个"],
                         require_report=True)


async def test_zero_case_and_missing_report_say_different_things(buggy_repo):
    """两种形状的诊断路径完全不同，消息不能混。

    没有报告 → 进程没跑完（超时被杀 / 崩溃 / 命令根本没执行起来）。
    报告为空 → 进程跑完了并正常退出，是**收集**没成功（node id 无效、
    conftest 抛异常、测试依赖缺失）。给错方向的排查提示比不给更费时间。
    """
    with pytest.raises(RuntimeError) as missing:
        await run_full_suite(buggy_repo, _SilentAdapter(), require_report=True)
    with pytest.raises(RuntimeError) as empty:
        await run_full_suite(buggy_repo, _ZeroCaseAdapter(),
                             require_report=True)
    assert "未产出任何 JUnit 报告" in str(missing.value)
    assert "收集" in str(empty.value)
    assert str(missing.value) != str(empty.value)


async def test_zero_case_report_is_still_tolerated_by_default(buggy_repo):
    """默认档不变：非 required 的调用方拿到空集合，不抛。"""
    fs = await run_full_suite(buggy_repo, _ZeroCaseAdapter())
    assert fs.ids == set() and set(fs.ran) == set()


async def test_missing_report_message_names_no_particular_file(buggy_repo):
    """报告可以有多份（Maven surefire 每个测试类一份）。

    消息里点名某一个文件，在多报告适配器上就是一句假话 —— 本项目把
    「消息说了一件代码没做的事」与「数字造假」同等对待。
    """
    with pytest.raises(RuntimeError) as ei:
        await run_full_suite(buggy_repo, _SilentAdapter(), require_report=True)
    msg = str(ei.value)
    assert ".xml" not in msg, f"消息里点名了具体报告文件：{msg}"
    assert str(buggy_repo) in msg, f"消息没说是哪个 worktree：{msg}"


async def test_run_full_suite_result_is_unchanged_by_the_refactor(buggy_repo):
    """行为不变基准：接口从「一个路径」改成「一组路径」前实测的失败集。

    基准值由改造前的代码在同一个 buggy_repo 夹具上真跑一次得到，逐点写死。
    接口重构最典型的失败形状是「测试全绿，但某条路径悄悄少解析了一份报告」——
    只断言 ids 非空是发现不了的，所以连 ran / 字段值一起钉住。
    """
    fs = await run_full_suite(buggy_repo, PytestAdapter())
    assert fs.ids == {"tests/test_calc.py::test_add"}
    assert set(fs.ran) == {"tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"}
    f = fs.failures["tests/test_calc.py::test_add"]
    assert f.classname == "tests.test_calc"
    assert f.name == "test_add"
    assert f.file == "tests/test_calc.py"
    assert f.line == 3
    assert f.message == "assert -1 == 5\n +  where -1 = add(2, 3)"
    assert "add(2, 3)" in f.trace
    # 跑完不留产物：留在原地的报告会被下一跑的 report_paths 当成自己的结果
    assert list(buggy_repo.glob(".aifix-*.xml")) == []


async def test_run_scoped_result_is_unchanged_by_the_refactor(buggy_repo):
    """同上，scoped 那条路径的基准。两个用例都点名跑，只有 test_add 该红。"""
    fs = await run_scoped(buggy_repo, PytestAdapter(),
                          ["tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"])
    assert fs.ids == {"tests/test_calc.py::test_add"}
    assert set(fs.ran) == {"tests/test_calc.py::test_add",
                           "tests/test_calc.py::test_identity"}
    assert list(buggy_repo.glob(".aifix-*.xml")) == []


async def test_scoped_run_does_not_clobber_the_full_report(buggy_repo):
    """复跑写的是另一份报告 —— 否则全量那份被覆盖后又被清理删掉。

    flaky 复跑就发生在全量结果还要继续用的时候，覆盖等于把 baseline 换成
    一份只含两三个用例的报告。
    """
    a = PytestAdapter()
    sentinel = "<testsuites/>"
    (buggy_repo / a.REPORT_NAME).write_text(sentinel, encoding="utf-8")
    try:
        await run_scoped(buggy_repo, a, ["tests/test_calc.py::test_identity"])
        assert (buggy_repo / a.REPORT_NAME).read_text(encoding="utf-8") == sentinel
    finally:
        (buggy_repo / a.REPORT_NAME).unlink(missing_ok=True)


async def test_baseline_refuses_to_read_a_dead_test_run_as_all_green(
        buggy_repo, monkeypatch):
    """baseline 的测试进程没跑成时必须抛，不能安静地产出一个空队列。

    `run_full_suite` 默认容忍报告缺失，理由写在它的 docstring 里：「下一轮
    verify 会重新跑」。那句话对 verify 成立，对 baseline **不成立** ——
    baseline 一次 run 只跑一次，没有下一轮。它一旦返回空集合，队列就是空的，
    整个 run 以「修复 0 / 0、全绿、没活干」收场，退出码 0。

    这条路真实发生过：aifix 装成 uv tool 之后自带的解释器里没有 pytest，
    `sys.executable -m pytest` 直接失败、一份报告都没写，而报告显示一切正常。
    """
    from aifix.nodes import baseline as baseline_mod
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        monkeypatch.setattr(baseline_mod, "adapter_from_state",
                            lambda state: _SilentAdapter())
        with pytest.raises(RuntimeError, match="报告"):
            await baseline_node(st)


async def test_baseline_on_green_repo_yields_empty_queue(buggy_repo, fixed_source):
    (buggy_repo / "calc.py").write_text(fixed_source, encoding="utf-8")
    import subprocess
    subprocess.run(["git", "commit", "-qam", "fix"], cwd=buggy_repo, check=True)
    st = new_state(buggy_repo, AifixConfig(), run_id="r1")
    st.update(preflight_node(st))
    with Worktree(buggy_repo, run_id="r1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert out["queue"] == []
