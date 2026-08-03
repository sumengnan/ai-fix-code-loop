"""一次 run 同时跑两套测试套件。

改造前另一套的用例在 baseline 里**根本不存在**，于是 verify 的三态比较永远不会
因为它们变红而判 WORSE ——「没测」被显示成了「通过」。这个文件钉的就是那件事。

真跑那条（前后端同仓）挂了 skip，判据与 vitest 适配器那边同款：`npm install
--offline` 装得上就跑。纯逻辑的几条不挂。
"""
from __future__ import annotations

import json
import subprocess

import pytest

from aifix.adapters.base import Failure, FailureSet, merge, tag_owner
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.adapters.vitest_adapter import VitestAdapter
from aifix.config import AifixConfig
from aifix.graph import new_state
from aifix.nodes.baseline import (_dispatch, adapter_for_test, run_full_suite,
                                  run_scoped)
from aifix.nodes.preflight import preflight_node
from test_vitest_adapter import _vitest_skip


def _f(tid: str) -> Failure:
    return Failure(test_id=tid, classname="c", name="n", message="", trace="")


# ------------------------------------------------------------------ 记账

def test_owner_covers_passing_cases_too():
    """**通过的用例也要记账。**

    复跑一条当前没红的用例（flaky 确认就在做这件事）同样要知道问哪个适配器，
    而它不在 failures 里。只记失败的话那条路会落进「没有 owner」，在多适配器
    仓库上直接抛异常。
    """
    fs = tag_owner(FailureSet({"a::x": _f("a::x")},
                              ran=frozenset({"a::x", "a::y"})), "pytest")
    assert fs.owner == {"a::x": "pytest", "a::y": "pytest"}


def test_merge_keeps_both_sides():
    left = tag_owner(FailureSet({"t.py::x": _f("t.py::x")},
                                ran=frozenset({"t.py::x"})), "pytest")
    right = tag_owner(FailureSet({"a.test.ts::y": _f("a.test.ts::y")},
                                 ran=frozenset({"a.test.ts::y"})), "vitest")
    both = merge([left, right])
    assert both.ids == {"t.py::x", "a.test.ts::y"}
    assert both.ran == {"t.py::x", "a.test.ts::y"}
    assert both.owner == {"t.py::x": "pytest", "a.test.ts::y": "vitest"}


def test_the_three_state_verdict_needs_no_change():
    """`compare()` 是纯集合运算，把两份并起来喂进去就成立。

    这条钉的是一个**设计结论**而不是一行代码：判定层不认识适配器，所以多适配器
    没有在那里留任何缺口。真有人往 compare 里塞进语言相关的判断时，这条会红。
    """
    from aifix.adapters.base import Verdict
    from aifix.verify import compare

    base = merge([
        tag_owner(FailureSet({"t.py::x": _f("t.py::x")}), "pytest"),
        tag_owner(FailureSet({"a.test.ts::y": _f("a.test.ts::y")}), "vitest")])
    # 后端修好了，前端仍然红着 —— 目标是后端那条
    cur = merge([
        tag_owner(FailureSet({}), "pytest"),
        tag_owner(FailureSet({"a.test.ts::y": _f("a.test.ts::y")}), "vitest")])
    assert compare(base, cur, "t.py::x") is Verdict.BETTER

    # 前端**新**红了一条 → 一律 WORSE，哪怕目标修好了。
    # 这正是改造前拿不到的判定：那条用例压根不在 baseline 里。
    worse = merge([
        tag_owner(FailureSet({}), "pytest"),
        tag_owner(FailureSet({"a.test.ts::y": _f("a.test.ts::y"),
                              "a.test.ts::z": _f("a.test.ts::z")}), "vitest")])
    assert compare(base, worse, "t.py::x") is Verdict.WORSE


# ------------------------------------------------------------------ 派发

def test_dispatch_groups_by_owner():
    py, vi = PytestAdapter(), VitestAdapter()
    got = _dispatch(["t.py::a", "s.test.ts::b", "t.py::c"], [py, vi],
                    {"t.py::a": "pytest", "s.test.ts::b": "vitest",
                     "t.py::c": "pytest"})
    assert [(a.name, ids) for a, ids in got] == [
        ("pytest", ["t.py::a", "t.py::c"]), ("vitest", ["s.test.ts::b"])]


def test_one_adapter_needs_no_bookkeeping():
    """今天绝大多数仓库的形状。要求红检、fixer 的 run_tests 凭空造一份记账
    是纯粹的负担。"""
    got = _dispatch(["随便什么"], [PytestAdapter()], None)
    assert [(a.name, ids) for a, ids in got] == [("pytest", ["随便什么"])]


def test_a_missing_owner_raises_instead_of_guessing():
    """**不猜。** 猜错在三种适配器上都是静默的：id 进了另一套体系的命令行，
    那套体系不报错，只是一个用例都不跑、写出 tests="0" 的报告 —— 而复跑「跑了
    个空」会被 filter_flaky 读成「重跑就绿」，于是真的被补丁弄红的用例被划进
    抖动、从判定里剔除。
    """
    with pytest.raises(RuntimeError, match="不知道"):
        _dispatch(["无主的::x"], [PytestAdapter(), VitestAdapter()], {})


def test_adapter_for_test_uses_the_bookkeeping(tmp_path):
    st = new_state(tmp_path, AifixConfig(), run_id="r1")
    st["adapter_names"] = ["pytest", "vitest"]
    st["_owners"] = {"t.py::a": "pytest", "s.test.ts::b": "vitest"}
    assert adapter_for_test(st, "t.py::a").name == "pytest"
    assert adapter_for_test(st, "s.test.ts::b").name == "vitest"
    with pytest.raises(RuntimeError, match="不知道"):
        adapter_for_test(st, "谁都不认识的::x")


# --------------------------------------------------------------- preflight

def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n",
                                             encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^2"}}), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_without_config_only_the_first_adapter_is_used(tmp_path):
    """**默认行为与改造前逐字节相同。**

    自动「探测到几个就跑几个」不成立：`PytestAdapter.detect` 极宽松，而 Java
    工程带 Python 胶水脚本是常事 —— 那类仓库会凭空多跑一套 pytest、收不到任何
    用例，然后被 require_report 判成「测试没跑成」当场中止。一个今天能正常
    工作的仓库在升级之后打不开，是这次改动最该避免的后果。
    """
    repo = _git_repo(tmp_path)
    out = preflight_node(new_state(repo, AifixConfig(), run_id="r1"))
    assert out["adapter_names"] == ["vitest"], "注册表顺序里 vitest 在 pytest 前"


def test_explicit_config_turns_on_both(tmp_path):
    repo = _git_repo(tmp_path)
    cfg = AifixConfig(adapters=("pytest", "vitest"))
    out = preflight_node(new_state(repo, cfg, run_id="r1"))
    assert out["adapter_names"] == ["pytest", "vitest"]


def test_a_typo_in_the_config_is_refused_at_preflight(tmp_path):
    """写错的名字要在这里拒，不能等到 baseline —— 那时用户看到的会是
    「测试没跑成」，一句指向目标项目的话。"""
    repo = _git_repo(tmp_path)
    cfg = AifixConfig(adapters=("pytest", "vitset"))
    out = preflight_node(new_state(repo, cfg, run_id="r1"))
    assert out["abort"] is not None
    assert "vitset" in out["abort"]
    assert out["abort_kind"] is not None, "只写 abort 会让 run 静默退 0"


def test_the_config_accepts_a_comma_separated_string(monkeypatch):
    """环境变量只能是字符串。pydantic-settings 会先拿它去 JSON 解码，
    没有 NoDecode 的话 `pytest,vitest` 一填就是 JSONDecodeError。"""
    monkeypatch.setenv("AIFIX_ADAPTERS", "pytest,vitest")
    assert AifixConfig().adapters == ("pytest", "vitest")


# ------------------------------------------------------------------- 真跑

_PKG = {"name": "fullstack-probe", "private": True, "type": "module"}


@_vitest_skip()
async def test_both_suites_really_run_and_both_failures_show_up(tmp_path):
    """**整件事的验收：前后端同仓，两套测试都真的跑，两侧的红都进 baseline。**

    改造前另一侧的用例在 baseline 里根本不存在 —— 不是「通过」，是没测。
    这条断言的是两侧的失败**同时**出现在一份结果里，且各自带着正确的出处。
    """
    subprocess.run(["npm", "install", "--offline", "--no-audit", "--no-fund",
                    "--silent", "vitest"], cwd=tmp_path, check=True,
                   capture_output=True, timeout=180)
    (tmp_path / "package.json").write_text(json.dumps(_PKG), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n",
                                         encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_back.py").write_text(
        "def test_backend_red():\n    assert 1 == 2\n"
        "def test_backend_green():\n    assert 1 == 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "front.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('frontend red', () => { expect(1).toBe(2) })\n"
        "it('frontend green', () => { expect(1).toBe(1) })\n", encoding="utf-8")

    adapters = [PytestAdapter(), VitestAdapter()]
    fs = await run_full_suite(tmp_path, adapters, require_report=True)

    back = "tests/test_back.py::test_backend_red"
    front = "src/front.test.ts::frontend red"
    assert back in fs.ids, sorted(fs.ids)
    assert front in fs.ids, sorted(fs.ids)
    assert fs.owner[back] == "pytest"
    assert fs.owner[front] == "vitest"
    # 通过的那两条也要在 ran 里带出处 —— 复跑它们时要靠这个派发
    assert fs.owner["tests/test_back.py::test_backend_green"] == "pytest"
    assert fs.owner["src/front.test.ts::frontend green"] == "vitest"

    # 复跑只跑点到名的那一侧，不该顺带把另一整套也跑一遍
    only_front = await run_scoped(tmp_path, adapters, [front],
                                  require_report=True, owner=fs.owner)
    assert only_front.ran == {front}, sorted(only_front.ran)
