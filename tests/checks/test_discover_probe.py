"""第二、三层：让模型给一对候选写对比代码，再跑一遍证伪。

语义与复现那一步**完全一致**，所以整条链原样复用：

    对比测试必须**红** —— 红 = 两处对同一输入给了不同答案 = 缺陷
    绿                 —— 两处一致，这个候选作废，静默丢弃

于是 `red_check` 的四道闸白捡：红在收集错误上、红在自己的笔误上、根本没跑起来
——这几种「红得没有信息量」在这里同样是「这次探测不算数」。

三种收场必须分开，因为下一步动作完全不同：

    ok             —— 确认不一致，落成一条红着的测试，进修复循环
    agreed         —— 两边一致，候选作废。**这不是失败**，是这一层最常见的结果
    not_comparable —— 模型说这两个压根不是一回事（第一层是启发式的，会配错）
"""
import json

import pytest

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.discover.probe import KIND_AGREED, KIND_NOT_COMPARABLE, KIND_OK
from aifix.discover.twins import Site, Twin

_TWIN = Twin(
    a=Site("exam_grader.py", "grade_objective", 1),
    b=Site("quiz_service.py", "grade", 1),
    shared_literals=frozenset({"single", "multiple", "truefalse"}),
    shared_roots=frozenset({"grade"}),
)


# ---------------------------------------------------------------- 提示词

def test_the_prompt_carries_both_sites():
    """模型要能直接跳过去读那两段代码。"""
    from aifix.agents.twin_prober import build_prompt

    p = build_prompt(_TWIN, ["tests"])
    for s in (_TWIN.a, _TWIN.b):
        assert s.path in p and s.name in p


def test_the_prompt_says_why_these_two_were_paired():
    """不说依据的话，模型无从判断「它们是不是真的一回事」——而那正是
    它在这一层唯一能做的判断（第一层是启发式的，会配错）。"""
    from aifix.agents.twin_prober import build_prompt

    p = build_prompt(_TWIN, ["tests"])
    assert "grade" in p and "multiple" in p


def test_the_prompt_demands_a_failing_test_not_a_report():
    """要的是一段能跑的对比，不是一句「我觉得这里有问题」。"""
    from aifix.agents.twin_prober import SYSTEM_PROMPT

    assert "同一份输入" in SYSTEM_PROMPT
    assert "断言" in SYSTEM_PROMPT
    assert "自包含" in SYSTEM_PROMPT


def test_the_prompt_lets_the_model_reject_the_pair():
    """第一层是启发式的。模型必须能说「这两个不是一回事」，否则它只能硬编
    一个对比出来 —— 那就是凭空造一个假缺陷。"""
    from aifix.agents.twin_prober import SYSTEM_PROMPT

    assert "不是同一件事" in SYSTEM_PROMPT or "不可比" in SYSTEM_PROMPT


# ---------------------------------------------------------------- 解析

def _raw(**over) -> str:
    return json.dumps({
        "can_probe": True,
        "test_file": "tests/test_twin_grade.py",
        "test_code": ("from exam_grader import grade_objective\n"
                      "from quiz_service import grade\n\n\n"
                      "def test_two_graders_agree():\n"
                      "    q = {'type': 'multiple', 'answer': [0, 1]}\n"
                      "    assert grade_objective(q, [0, 0, 1]) == grade(q, [0, 0, 1])\n"),
        "target_test_id": "tests/test_twin_grade.py::test_two_graders_agree",
        "not_comparable_why": "",
    } | over)


def test_a_well_formed_probe_parses():
    from aifix.agents.twin_prober import parse_probe

    r, why = parse_probe(_raw(), PytestAdapter().is_test_path)
    assert why == "" and r is not None
    assert r.target_test_id.endswith("::test_two_graders_agree")


def test_rejecting_the_pair_needs_a_reason():
    """说了不可比却不说为什么，等于什么都没说 —— 而那句说明是这条通路
    唯一的产出（它会决定这一对要不要从候选里永久划掉）。"""
    from aifix.agents.twin_prober import parse_probe

    _, why = parse_probe(_raw(can_probe=False, not_comparable_why=""),
                         PytestAdapter().is_test_path)
    assert why

    r, why2 = parse_probe(
        _raw(can_probe=False, not_comparable_why="一个渲染文本、一个判对错"),
        PytestAdapter().is_test_path)
    # 归一成 Reproduction：下游原样复用复现那条通路，不可比 = 写不出复现，
    # 理由进 missing_info。
    assert why2 == "" and r.can_reproduce is False
    assert r.missing_info == ["一个渲染文本、一个判对错"]


def test_the_self_containment_gate_applies_here_too():
    """对比测试同样落进一个新文件 —— 复用复现那一层的自包含校验。"""
    from aifix.agents.twin_prober import parse_probe

    _, why = parse_probe(
        _raw(test_code="def test_x():\n    assert grade_objective(q) == grade(q)\n"),
        PytestAdapter().is_test_path)
    assert "自包含" in why


# ---------------------------------------------------------------- 三种收场

@pytest.fixture
def twin_repo(tmp_path):
    """两个判分器：多选那一支的写法不同（一个去重、一个不去重）。

    三个分派键不是凑数：`find_twins` 默认要求共享 ≥3 个，少于这个数的夹具
    走 `scan_and_fix` 时会扫不出候选 —— 而那会让整条链的测试**空过**。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "exam_grader.py").write_text(
        "def grade_objective(q, ans):\n"
        "    if q['type'] == 'truefalse':\n"
        "        return ans is bool(q['answer'])\n"
        "    if q['type'] == 'single':\n"
        "        return ans == q['answer']\n"
        "    if q['type'] == 'multiple':\n"
        "        return sorted(set(ans or [])) == sorted(set(q['answer'] or []))\n"
        "    return False\n", encoding="utf-8")
    (tmp_path / "quiz_service.py").write_text(
        "def grade(q, ans):\n"
        "    if q['type'] == 'truefalse':\n"
        "        return ans is bool(q['answer'])\n"
        "    if q['type'] == 'single':\n"
        "        return ans == q['answer']\n"
        "    if q['type'] == 'multiple':\n"
        "        return sorted(ans or []) == sorted(q['answer'])\n"
        "    return False\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    return tmp_path


_DIVERGING = json.dumps({
    "can_probe": True,
    "test_file": "tests/test_twin.py",
    "test_code": ("from exam_grader import grade_objective\n"
                  "from quiz_service import grade\n\n\n"
                  "def test_agree():\n"
                  "    q = {'type': 'multiple', 'answer': [0, 1]}\n"
                  "    assert grade_objective(q, [0, 0, 1]) == grade(q, [0, 0, 1])\n"),
    "target_test_id": "tests/test_twin.py::test_agree",
    "not_comparable_why": "",
})

_AGREEING = json.dumps({
    "can_probe": True,
    "test_file": "tests/test_twin.py",
    "test_code": ("from exam_grader import grade_objective\n"
                  "from quiz_service import grade\n\n\n"
                  "def test_agree():\n"
                  "    q = {'type': 'multiple', 'answer': [0, 1]}\n"
                  "    assert grade_objective(q, [0, 1]) == grade(q, [0, 1])\n"),
    "target_test_id": "tests/test_twin.py::test_agree",
    "not_comparable_why": "",
})


async def test_a_real_divergence_is_confirmed(twin_repo, scripted):
    """两边给不同答案 → 对比测试红 → 这是一个确认过的缺陷。"""
    from aifix.discover.probe import probe_twin

    out = await probe_twin(twin_repo, PytestAdapter(), _TWIN,
                           client=scripted(_DIVERGING))
    assert out.kind == KIND_OK, out.reason
    assert out.reproduction is not None
    # 产出直接就是修复循环要的东西
    assert out.reproduction.target_test_id == "tests/test_twin.py::test_agree"


async def test_agreement_is_not_a_failure(twin_repo, scripted):
    """两边一致 → 候选作废。这是最常见的结果，不该报成错误。"""
    from aifix.discover.probe import probe_twin

    out = await probe_twin(twin_repo, PytestAdapter(), _TWIN,
                           client=scripted(_AGREEING))
    assert out.kind == KIND_AGREED
    assert out.reproduction is None


async def test_the_probe_file_is_removed_when_they_agree(twin_repo, scripted):
    """一致的候选不留痕：那个文件留在工作区里会被下一次 baseline 收进去。"""
    from aifix.discover.probe import probe_twin

    await probe_twin(twin_repo, PytestAdapter(), _TWIN,
                     client=scripted(_AGREEING))
    assert not (twin_repo / "tests" / "test_twin.py").exists()


async def test_the_model_can_reject_the_pair(twin_repo, scripted):
    from aifix.discover.probe import probe_twin

    raw = json.dumps({"can_probe": False, "not_comparable_why":
                      "一个渲染文本、一个判对错，返回类型都不同"})
    out = await probe_twin(twin_repo, PytestAdapter(), _TWIN,
                           client=scripted(raw))
    assert out.kind == KIND_NOT_COMPARABLE
    assert "返回类型" in out.reason


async def test_a_probe_that_is_red_for_its_own_typo_does_not_count(
        twin_repo, scripted):
    """红检那四道闸原样生效：红在自己的 NameError 上不算发现了不一致。"""
    from aifix.discover.probe import probe_twin

    raw = json.dumps({
        "can_probe": True,
        "test_file": "tests/test_twin.py",
        "test_code": ("import pytest\n\n\n"
                      "def test_agree():\n"
                      "    with pytest.raises(ValueError):\n"
                      "        no_such_name()\n"),
        "target_test_id": "tests/test_twin.py::test_agree",
        "not_comparable_why": "",
    })
    out = await probe_twin(twin_repo, PytestAdapter(), _TWIN,
                           client=scripted(raw))
    assert out.kind not in (KIND_OK,)
    assert out.reason


# ------------------------------------------------- 三级递进：列 / 确认 / 修

def test_scan_alone_costs_nothing():
    """`aifix scan` 不带任何标志时**零模型调用**：它只是一份候选清单。
    要花钱、要动仓库，都必须显式说。"""
    from aifix.cli import build_parser

    a = build_parser().parse_args(["scan", "--repo", "."])
    assert a.probe is False and a.fix is False


def test_fix_implies_probe():
    """没确认过就去修，等于对着一堆启发式候选花钱。"""
    from aifix.cli import build_parser

    a = build_parser().parse_args(["scan", "--repo", ".", "--fix"])
    assert a.fix is True


def test_there_is_a_hard_cap_on_how_many_candidates_get_probed():
    """每个候选都是一次模型调用。没有上限的话，一个大仓库扫出几十对就是
    几十次调用，而这一层的产出多半是「一致，作废」。"""
    from aifix.cli import build_parser

    a = build_parser().parse_args(["scan", "--repo", "."])
    assert a.max_probes > 0


async def test_only_confirmed_divergences_reach_the_fix_loop(
        twin_repo, scripted, monkeypatch):
    """agreed / not_comparable 都不该进修复循环 —— 那是在修一个不存在的缺陷。"""
    from aifix.discover import scan as scan_mod

    called: list[str] = []

    async def _fake_run_once(repo, config, run_id, only_test=None, **kw):
        called.append(only_test)
        return {"results": [], "report_md": "", "baseline_ids": []}

    outs = await scan_mod.scan_and_fix(
        twin_repo, config=None, client=scripted(_AGREEING),
        run_fn=_fake_run_once, do_fix=True)
    assert called == [], "两边一致时不该起修复循环"
    assert all(o.kind != KIND_OK for o in outs)


async def test_a_confirmed_divergence_is_committed_before_the_fix_loop(
        twin_repo, scripted, monkeypatch):
    """测试必须先进 HEAD：worktree 从 HEAD 建，不 commit 的话 baseline 认不出
    它，队列是空的，run 以「没活干」正常收场而报告说「你的仓库没问题」。"""
    import subprocess

    from aifix.discover import scan as scan_mod

    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@example.com"],
                ["config", "user.name", "t"], ["add", "-A"],
                ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=twin_repo, check=True)

    seen: list[str] = []

    async def _fake_run_once(repo, config, run_id, only_test=None, **kw):
        # 跑到这里时，那条测试必须已经在 HEAD 里
        out = subprocess.run(["git", "ls-files", "--", "tests/test_twin.py"],
                             cwd=repo, capture_output=True, text=True)
        seen.append(out.stdout.strip())
        return {"results": [], "report_md": "", "baseline_ids": []}

    await scan_mod.scan_and_fix(twin_repo, config=None,
                                client=scripted(_DIVERGING),
                                run_fn=_fake_run_once, do_fix=True)
    assert seen == ["tests/test_twin.py"]


async def test_probe_only_never_touches_git(twin_repo, scripted):
    """`--probe` 只确认、不修：它不该 commit 任何东西。"""
    from aifix.discover import scan as scan_mod

    async def _boom(*a, **k):
        raise AssertionError("--probe 不该起修复循环")

    outs = await scan_mod.scan_and_fix(twin_repo, config=None,
                                       client=scripted(_DIVERGING),
                                       run_fn=_boom, do_fix=False)
    assert [o.kind for o in outs] == [KIND_OK]


def test_the_probe_does_not_force_json_output():
    """**不套 `json_output`**：这一轮里模型要先调工具，而强制整轮输出 JSON
    会和工具调用互相干扰。

    实测（deepseek-v4-pro，第一次真跑）：模型的分析完全正确，但它想调
    `list_files` 时吐的是厂商私有的 `<｜｜DSML｜｜tool_calls>` 文本格式而不是
    OpenAI 兼容的 tool_calls 字段 —— 两个候选都以 `unparseable` 收场，
    4401 tokens 白烧。

    reproduce 那一步早就记下了同一条（见 `reproduce.reproduce` 的 docstring），
    这里是同一个形状：有工具就不能强制 JSON，容错交给围栏剥离。

    直接考源码而不是行为：这个约束的表现是「跑起来才发现工具调用退化成文本」，
    没有便宜的运行时判据。
    """
    import inspect

    from aifix.discover import probe

    # 判「有没有被调用」而不是「文本里出现过」：解释这条约束的注释里也会
    # 写到这个名字，按裸子串判会把一条正确的注释算成违规。
    src = inspect.getsource(probe.probe_twin)
    assert "with json_output()" not in src, (
        "probe_twin 套了 json_output —— 这一轮有工具，会把工具调用挤成文本")
    imports = [ln for ln in src.splitlines()
               if "import" in ln and "json_output" in ln]
    assert not imports, f"还 import 着 json_output：{imports}"
