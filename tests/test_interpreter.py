"""跑目标项目测试用的解释器：显式配置 > 源仓库里的 venv > sys.executable。

这一组测试钉的是**可用性**：`sys.executable` 意味着「目标项目的测试依赖必须
装在 aifix 自己的解释器里」，而真实项目从来不满足这一条。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from aifix.adapters.maven_adapter import MavenAdapter
from aifix.adapters.pytest_adapter import (PytestAdapter,
                                           discover_test_python,
                                           imports_outside_worktree,
                                           resolve_test_parallel,
                                           resolve_test_python)
from aifix.config import AifixConfig


def _fake_venv(repo: Path, dirname: str = ".venv") -> Path:
    """在 repo 里造一个**能通过可执行性检查**的假解释器。"""
    exe = repo / dirname / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n",
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


# --------------------------------------------------------------------------
# 1. 适配器：解释器是构造参数，命令逐点不变
# --------------------------------------------------------------------------

_BASE = ["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-o", "junit_family=xunit1"]


def test_default_command_is_byte_for_byte_what_it_was():
    """回归断言：不传解释器时，两条命令与改造之前**逐点一致**。

    不写成 `cmd[0] == sys.executable` —— 那样的话，命令中间任何一项被顺手
    改掉都不会有人发现，而 `-B` / `-p no:cacheprovider` / `junit_family=xunit1`
    每一项都是踩出来的（见 PytestAdapter._BASE 的注释）。
    """
    a = PytestAdapter()
    assert a.full_test_command() == [
        sys.executable, *_BASE, "--junitxml=.aifix-report.xml"]
    assert a.scoped_test_command(["tests/test_x.py::t"]) == [
        sys.executable, *_BASE, "--junitxml=.aifix-recheck.xml",
        "tests/test_x.py::t"]


def test_injected_interpreter_replaces_only_argv0():
    """注入的解释器只换掉 argv[0]，其余每一项原样保留。"""
    a = PytestAdapter(python="/opt/proj/.venv/bin/python")
    assert a.full_test_command() == [
        "/opt/proj/.venv/bin/python", *_BASE, "--junitxml=.aifix-report.xml"]
    assert a.scoped_test_command(["t::x"]) == [
        "/opt/proj/.venv/bin/python", *_BASE,
        "--junitxml=.aifix-recheck.xml", "t::x"]


def test_maven_command_is_unaffected_by_the_interpreter():
    """mvn 是外部命令，不走解释器 —— 但构造参数要收下，否则注册表构造会炸。

    `adapter_for` 对两个适配器用的是同一行 `ADAPTERS[name](python=...)`：
    Maven 不接这个参数的话，任何 Maven 工程都会在取适配器时 TypeError。
    """
    assert MavenAdapter(python="/opt/proj/.venv/bin/python"
                        ).full_test_command() == MavenAdapter(
        ).full_test_command()
    assert MavenAdapter().full_test_command()[0] == "mvn"


# --------------------------------------------------------------------------
# 2. 探测：只看源仓库，且只认真的能执行的文件
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dirname", [".venv", "venv"])
def test_discovers_both_venv_layouts(tmp_path, dirname):
    exe = _fake_venv(tmp_path, dirname)
    assert discover_test_python(tmp_path) == str(exe)


def test_dot_venv_wins_over_venv(tmp_path):
    """两个都在时取 `.venv` —— uv / poetry / python -m venv 的现代默认。"""
    dot = _fake_venv(tmp_path, ".venv")
    _fake_venv(tmp_path, "venv")
    assert discover_test_python(tmp_path) == str(dot)


def test_no_venv_means_no_candidate(tmp_path):
    assert discover_test_python(tmp_path) is None


def test_a_non_executable_file_is_not_an_interpreter(tmp_path):
    """`.venv/bin/python` 存在但不可执行 → 不算候选。

    这条不是洁癖：拿它当命令的 argv[0] 会在 exec 时 PermissionError，而那
    发生在 baseline 里 —— 用户看到的是一次「测试没跑成」的中止，而不是
    「你的 .venv 是坏的」。退回 sys.executable 至少不比现在差。
    """
    exe = tmp_path / ".venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o644)
    assert discover_test_python(tmp_path) is None


# --------------------------------------------------------------------------
# 3. 优先级：显式 > 探测 > sys.executable
# --------------------------------------------------------------------------

def test_explicit_config_beats_discovery(tmp_path):
    """仓库里**真的有**一个能用的 venv，显式配置仍然赢。

    造一个真候选（而不是空目录）才有区分度：仓库里没有 venv 时，
    「显式优先」和「压根没实现探测」给出的是同一个答案。
    """
    found = _fake_venv(tmp_path)
    got = resolve_test_python(tmp_path, "/usr/local/bin/python3.12")
    assert got == "/usr/local/bin/python3.12"
    assert got != str(found)


def test_discovery_beats_sys_executable(tmp_path):
    """没配置时用探测到的那个，而不是 sys.executable。"""
    found = _fake_venv(tmp_path)
    got = resolve_test_python(tmp_path, None)
    assert got == str(found)
    assert got != sys.executable


def test_nothing_configured_and_nothing_found_falls_back(tmp_path):
    """两条来源都空 → 返回 None，适配器据此退回 sys.executable（现状）。"""
    assert resolve_test_python(tmp_path, None) is None
    assert PytestAdapter(python=None).full_test_command()[0] == sys.executable


def test_env_var_feeds_the_config(monkeypatch):
    monkeypatch.setenv("AIFIX_TEST_PYTHON", "/opt/x/bin/python")
    assert AifixConfig().test_python == "/opt/x/bin/python"
    monkeypatch.delenv("AIFIX_TEST_PYTHON")
    assert AifixConfig().test_python is None


# --------------------------------------------------------------------------
# 4. 可编辑安装陷阱的探测
# --------------------------------------------------------------------------

def _pkg(root: Path, rel: str, name: str) -> Path:
    d = root / rel / name if rel else root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return d


def test_package_resolved_inside_the_worktree_is_not_reported(tmp_path):
    """扁平布局：`python -m pytest` 的 cwd 就在 sys.path 最前，worktree 赢。"""
    wt = tmp_path / "tree"
    _pkg(wt, "", "mypkg")
    assert imports_outside_worktree(sys.executable, wt) == []


def test_editable_install_shadowing_the_worktree_is_reported(tmp_path,
                                                             monkeypatch):
    """src 布局 + 没配 pythonpath → 导入落到 worktree 之外，必须报出来。

    用 PYTHONPATH 模拟 site-packages 里那条指向**源仓库**的可编辑安装记录
    （`__editable__*.pth` 就是往 sys.path 里塞一个绝对路径）。这正是这条
    修复引入的真实风险：解释器换成了目标项目的，而目标项目把自己装进了
    那个解释器 —— 测试跑的可能是没打补丁的源仓库代码。
    """
    wt = tmp_path / "tree"
    _pkg(wt, "src", "mypkg")
    outside = tmp_path / "source-repo" / "src"
    _pkg(outside, "", "mypkg")
    monkeypatch.setenv("PYTHONPATH", str(outside))

    hits = imports_outside_worktree(sys.executable, wt)
    assert [n for n, _ in hits] == ["mypkg"], hits
    assert str(outside) in hits[0][1]


def test_pythonpath_ini_puts_the_worktree_back_in_front(tmp_path, monkeypatch):
    """同一个仓库配上 `pythonpath = ["src"]` 就不该再报警。

    与上一条成对：只差 pyproject.toml 里那一行。缺了这一对，一个「永远
    返回空列表」的实现和一个「永远报警」的实现各能骗过其中一条。
    """
    wt = tmp_path / "tree"
    _pkg(wt, "src", "mypkg")
    (wt / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8")
    outside = tmp_path / "source-repo" / "src"
    _pkg(outside, "", "mypkg")
    monkeypatch.setenv("PYTHONPATH", str(outside))

    assert imports_outside_worktree(sys.executable, wt) == []


def test_pythonpath_from_pytest_ini_is_honoured_too(tmp_path, monkeypatch):
    """`pytest.ini` 里的 pythonpath 同样要认 —— 本仓库的夹具就是这么写的。"""
    wt = tmp_path / "tree"
    _pkg(wt, "src", "mypkg")
    (wt / "pytest.ini").write_text(
        "[pytest]\npythonpath = src\n", encoding="utf-8")
    outside = tmp_path / "source-repo" / "src"
    _pkg(outside, "", "mypkg")
    monkeypatch.setenv("PYTHONPATH", str(outside))

    assert imports_outside_worktree(sys.executable, wt) == []


def test_test_directories_are_not_probed(tmp_path):
    """`tests/` 是测试自己的包，不是被验证的产品代码，别拿它当锚点。"""
    wt = tmp_path / "tree"
    _pkg(wt, "", "tests")
    assert imports_outside_worktree(sys.executable, wt) == []


def test_a_broken_interpreter_never_takes_down_the_run(tmp_path):
    """探测本身是**建议性**的：解释器跑不起来时返回空，不抛。

    这道探测的价值是提醒，代价不能是「多一条崩溃路径」。
    """
    wt = tmp_path / "tree"
    _pkg(wt, "", "mypkg")
    assert imports_outside_worktree("/nonexistent/python", wt) == []


# --------------------------------------------------------------------------
# 5. 接线：核心循环真的用上了，而且探的是**源仓库**
# --------------------------------------------------------------------------

def _state(repo: Path, worktree: Path, config: AifixConfig):
    from aifix.graph import new_state
    st = new_state(repo, config, run_id="r1")
    st["adapter_names"] = ["pytest"]
    st["worktree_path"] = str(worktree)
    return st


def test_nodes_probe_the_source_repo_not_the_worktree(tmp_path):
    """探测目标是**源仓库**，不是 worktree。

    这条是整组里区分度最高的一条：worktree（`.aifix/runs/<id>/tree`，git
    worktree）和评测的 `git clone --local` 都**不含** `.venv`（它没被 git
    跟踪）。照着 worktree 探，探测永远为空，整个功能静默退化成 sys.executable
    而所有「显式配置优先」的测试照样绿。
    """
    from aifix.nodes.baseline import adapters_from_state

    repo = tmp_path / "repo"
    repo.mkdir()
    exe = _fake_venv(repo)
    worktree = tmp_path / "elsewhere" / "tree"
    worktree.mkdir(parents=True)
    assert discover_test_python(worktree) is None       # worktree 里确实没有

    adapter, = adapters_from_state(_state(repo, worktree, AifixConfig()))
    assert adapter.full_test_command()[0] == str(exe)


def test_nodes_let_the_explicit_config_win(tmp_path):
    from aifix.nodes.baseline import adapters_from_state

    repo = tmp_path / "repo"
    repo.mkdir()
    found = _fake_venv(repo)
    cfg = AifixConfig(test_python="/usr/local/bin/python3.12")
    adapter, = adapters_from_state(_state(repo, repo / "tree", cfg))
    assert adapter.full_test_command()[0] == "/usr/local/bin/python3.12"
    assert adapter.full_test_command()[0] != str(found)


def test_every_node_that_runs_tests_goes_through_the_same_entry():
    """四个节点必须都走那**两个**入口之一，否则解释器只对其中几个生效。

    fix 那条尤其致命：RunTestsTool 拿的是 fix_node 构造的适配器，模型自己
    跑的复跑会用另一个解释器 —— 与 verify 的判定依据不是同一套环境。

    多适配器落地之后入口是两个，而**两个都在 baseline 里注入解释器**：
    `adapters_from_state` 给要跑全量的节点（baseline / verify），
    `adapter_for_test` 给按单条 id 办事的节点（detect / fix）。绕过它们自己
    `adapter_for(...)` 的那条路仍然要堵死 —— 那里没有解释器。
    """
    import inspect

    from aifix.nodes import baseline, detect, fix, verify
    entries = ("adapters_from_state(state)", "adapter_for_test(state,")
    for mod in (baseline, detect, fix, verify):
        src = inspect.getsource(mod)
        assert any(e in src for e in entries), mod.__name__
        assert "adapter_for(state[" not in src, mod.__name__


def test_preflight_rejects_an_interpreter_that_is_not_there(buggy_repo):
    """显式配了一个不存在的解释器 → 启动即拒绝，别等到 baseline 才炸。

    到了 baseline 才发现的话，用户看到的是「测试未产出任何 JUnit 报告」——
    一句指向错误方向的话（它读起来像目标项目的测试挂了）。
    """
    from aifix.graph import new_state
    from aifix.nodes.preflight import preflight_node

    cfg = AifixConfig(test_python="/nope/bin/python")
    out = preflight_node(new_state(buggy_repo, cfg, run_id="r1"))
    assert out["abort"] and "/nope/bin/python" in out["abort"]


def test_preflight_accepts_a_real_interpreter(buggy_repo):
    """反向哨兵：配一个真存在的解释器不能被拒。"""
    from aifix.graph import new_state
    from aifix.nodes.preflight import preflight_node

    cfg = AifixConfig(test_python=sys.executable)
    out = preflight_node(new_state(buggy_repo, cfg, run_id="r1"))
    assert out["abort"] is None
    assert out["adapter_names"] == ["pytest"]


# 造真 venv 的 `real_venv` 工厂在 conftest.py：评测那条路（eval/runner）也要用
# 它做同样的端到端断言，两边各留一份会各自漂移。


async def test_baseline_really_runs_with_the_configured_interpreter(buggy_repo,
                                                                    real_venv):
    """端到端（显式配置）：baseline 真的用配的那个解释器跑出失败集合。"""
    from aifix.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes.baseline import baseline_node
    from aifix.nodes.preflight import preflight_node

    exe = real_venv(buggy_repo.parent / "sidecar-venv")
    st = new_state(buggy_repo, AifixConfig(test_python=str(exe)), run_id="ri1")
    st.update(preflight_node(st))
    assert st["abort"] is None
    with Worktree(buggy_repo, run_id="ri1") as wt:
        st["worktree_path"] = str(wt.path)
        out = await baseline_node(st)
    assert out["baseline_ids"] == ["tests/test_calc.py::test_add"], out


async def test_baseline_really_runs_with_the_discovered_venv(buggy_repo,
                                                             monkeypatch,
                                                             real_venv):
    """端到端（自动探测）：仓库里放一个 `.venv`，什么都不配也要用上它。

    这条把整条链路一次性钉死，而且钉的是最容易写错的那一环：`.venv` 在
    **源仓库**里，跑测试的 cwd 是 worktree，而 worktree 里没有 `.venv`
    （git 不跟踪它）—— 测试当场断言了这一点。照着 worktree 探的实现会在
    这里退回 sys.executable，而 sys.executable 被 monkeypatch 换成了一个
    跑不了 pytest 的解释器，于是当场红。
    """
    from aifix.delivery import Worktree
    from aifix.graph import new_state
    from aifix.nodes.baseline import baseline_node
    from aifix.nodes.preflight import preflight_node

    exe = real_venv(buggy_repo / ".venv")
    # 断掉回退路径：只有真的用上了探测到的解释器，测试才跑得起来
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")

    st = new_state(buggy_repo, AifixConfig(), run_id="ri2")
    st.update(preflight_node(st))
    assert st["abort"] is None
    with Worktree(buggy_repo, run_id="ri2") as wt:
        st["worktree_path"] = str(wt.path)
        assert not (wt.path / ".venv").exists()     # worktree 里确实没有
        out = await baseline_node(st)
    assert out["baseline_ids"] == ["tests/test_calc.py::test_add"], out
    assert str(exe).startswith(str(buggy_repo))


# ---------------------------------------------------------------- 并行全量

def test_parallel_is_off_by_default_so_the_command_is_unchanged():
    """不传 parallel 时，两条命令与并行化之前**逐字节相同**。

    这一条是回归闸：并行是个可选项，默认路径上一个字都不该多。
    """
    a = PytestAdapter(python="/p/py")
    assert a.full_test_command() == [
        "/p/py", *_BASE, "--junitxml=.aifix-report.xml"]


def test_parallel_only_affects_the_full_run_not_the_scoped_one():
    """**只并行全量。**

    scoped 一次就跑一两个用例，起 N 个 worker 是纯开销 —— xdist 要 fork 进程、
    收集、分发、汇总，而被分发的只有一个用例。flaky 确认那一跑尤其怕这个：
    它本来只花几秒。
    """
    a = PytestAdapter(python="/p/py", parallel="auto")
    assert "-n" in a.full_test_command()
    assert "-n" not in a.scoped_test_command(["t::x"])


def test_parallel_takes_the_worker_count_verbatim():
    """给数字就原样发下去 —— 「auto」在 CPU 多的机器上会开满，而 runner 上
    多数时候两个 worker 就够，再多是抢 CPU。"""
    cmd = PytestAdapter(python="/p/py", parallel="4").full_test_command()
    assert cmd[cmd.index("-n") + 1] == "4"


def test_the_junitxml_flag_survives_parallelism():
    """报告是判定的唯一取证 —— 并行参数插进去不能把它挤掉。"""
    cmd = PytestAdapter(python="/p/py", parallel="auto").full_test_command()
    assert "--junitxml=.aifix-report.xml" in cmd
    for item in _BASE:
        assert item in cmd, item


def test_an_unusable_parallel_value_falls_back_to_serial():
    """空串与 off 一律当没设。

    空串这条不能省：GitHub Actions 里 `env: X: ${{ vars.Y }}` 在 Y 未设置时
    给的是**空串**而不是不设 —— 不接这一手，`-n ''` 会被发给 pytest，而它
    以「argument -n: invalid int value」当场退出，表现成整个 baseline 跑不起来。
    """
    for bad in ("", "off", "0", "  "):
        cmd = PytestAdapter(python="/p/py", parallel=bad).full_test_command()
        assert "-n" not in cmd, repr(bad)


def test_resolve_parallel_prefers_the_explicit_value_over_probing():
    """给了具体数字就直接用，不去探 —— 那是用户明确的要求，
    不该被一次探测推翻（而探测本身要起一个子进程，不是免费的）。"""
    assert resolve_test_parallel("/nonexistent/python", "4") == "4"


def test_resolve_parallel_falls_back_to_serial_when_xdist_is_missing():
    """探不到 xdist 就串行 —— 而不是发一条 `-n auto` 让 pytest 以
    「unrecognized arguments: -n」当场退出，那会把整个 baseline 变成
    「测试进程没能正常跑完」。"""
    assert resolve_test_parallel("/definitely/not/a/python", "auto") is None


def test_resolve_parallel_finds_xdist_in_this_very_interpreter():
    """正向那一半：本仓库的 dev 依赖里就有 pytest-xdist，探得到。

    只断言 not-None，不断言等于 "auto" 之外的东西 —— 这一条要钉的是
    「探测真的会返回真」，而不是取值本身。
    """
    assert resolve_test_parallel(sys.executable, "auto") == "auto"


def test_resolve_parallel_is_off_unless_asked():
    """没配就串行。并行是可选项，默认路径一个字都不多。"""
    assert resolve_test_parallel(sys.executable, None) is None
    assert resolve_test_parallel(sys.executable, "") is None


def test_the_parallel_setting_reaches_the_actual_command(tmp_path):
    """**整条线**：config → adapters_from_state → 真正发出去的命令。

    断在任何一环，表现都是「并行一声不吭地没生效」—— 而这个功能唯一的
    观测方式就是「跑得快不快」，没人会因为慢就去查它是不是没接上。
    """
    from aifix.nodes.baseline import adapters_from_state

    state = {"adapter_names": ["pytest"], "repo": str(tmp_path),
             "config": AifixConfig(test_python=sys.executable,
                                   test_parallel="3")}
    adapter, = adapters_from_state(state)
    cmd = adapter.full_test_command()
    assert cmd[cmd.index("-n") + 1] == "3"


def test_turning_it_off_in_config_really_turns_it_off(tmp_path):
    """撞上 xdist-不安全的套件时，这个开关是唯一的出路 —— 它必须真的管用。"""
    from aifix.nodes.baseline import adapters_from_state

    state = {"adapter_names": ["pytest"], "repo": str(tmp_path),
             "config": AifixConfig(test_python=sys.executable,
                                   test_parallel="off")}
    adapter, = adapters_from_state(state)
    assert "-n" not in adapter.full_test_command()


def test_maven_takes_the_parallel_argument_without_choking(tmp_path):
    """`adapter_for` 对两个实现用同一行构造 —— Maven 不收这个参数的话，
    任何 Maven 工程都会在取适配器时 TypeError，而那发生在 baseline 之前。"""
    from aifix.nodes.baseline import adapter_for

    assert adapter_for("maven", python="/p/py", parallel="auto"
                       ).full_test_command() == MavenAdapter().full_test_command()
