import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure, FailureSet, Verdict
from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.verify import compare


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


def test_target_fixed_is_better():
    assert compare(_fs("a", "b"), _fs("b"), "a") is Verdict.BETTER


def test_nothing_changed_is_same():
    assert compare(_fs("a", "b"), _fs("a", "b"), "a") is Verdict.SAME


def test_new_failure_is_worse():
    assert compare(_fs("a"), _fs("a", "c"), "a") is Verdict.WORSE


def test_target_fixed_but_regression_is_worse():
    """核心约束：即使目标修好了，引入任何新失败一律 WORSE。"""
    assert compare(_fs("a", "b"), _fs("b", "c"), "a") is Verdict.WORSE


def test_other_failure_fixed_but_target_not_is_same():
    """顺手修好别的、目标没修好 —— 不算 BETTER。"""
    assert compare(_fs("a", "b"), _fs("a"), "a") is Verdict.SAME


def test_all_fixed_is_better():
    assert compare(_fs("a", "b"), _fs(), "a") is Verdict.BETTER


# —— 判 BETTER 之后：交付分支上到底有没有那个提交 ——
#
# compare() 只回答「失败集变好了没有」。它答 BETTER，不等于**这个 run 交付了
# 什么** —— 报告写「已修复」并给出 `git merge`，前提是分支上真的多了一个提交。
# 下面两条一正一反地钉住这件事，走的是完整的 run_once，因为要断言的三样东西
# （判定 / 报告 / 分支）分别产在三个不同的节点上。

# 与 test_e2e.py、test_signals_wiring.py 里的模型替身逐字相同。pytest 配了
# --import-mode=importlib 且 tests/ 不是包，测试模块之间 import 不到；共用的
# 去处只有 conftest.py，而那里不是放模型替身的地方 —— 于是照抄，不另造一套
# 形状不同的替身。


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


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _init_repo(repo: Path) -> str:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD").strip()


def _fact_keys(repo: Path, run_id: str) -> list[str]:
    p = repo / ".aifix" / "runs" / run_id / "facts.jsonl"
    return [json.loads(ln)["key"]
            for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b
'''

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})

# 首次运行必红、之后转绿 —— 「目标用例在 baseline 里本来就是抖的」的最小
# 复刻，而且是确定性的。标记文件是未跟踪的，每个 worktree 各自从零开始。
_FLAKY_TEST = '''import os

from calc import add

_MARK = os.path.join(os.path.dirname(__file__), ".seen")


def test_add():
    if not os.path.exists(_MARK):
        open(_MARK, "w").close()
        assert add(2, 3) == 5
'''

_FIX_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+    return a + b\n"
)

# 反向补丁：把上一条原样改回去。touched 里有 calc.py，工作区却与 HEAD 逐字
# 相同 —— 模型「改了又改回来」的最短路径。
_UNFIX_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    return a - b        # bug: 应为 a + b\n"
)


@pytest.fixture
def flaky_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "flaky"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_FLAKY_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _init_repo(repo)
    return repo


async def test_a_self_cancelling_patch_is_not_reported_as_fixed(flaky_repo):
    """打了补丁又打了反向补丁：判定必须是 same，报告不许写「已修复」。

    这是「不崩溃、不报错、测试全绿，只有承诺是假的」的第二种形状。第一种
    （touched 记成 `a/calc.py`，git add 匹配不到）已经被挡住了；这一种连
    touched 都是对的 —— 路径就在、文件就在，只是内容与 HEAD 一模一样。于是
    「touched 非空」那道守卫放行、commit 什么都没暂存、安静返回，报告照写
    「修复 1 / 1」并给出 `git merge`，而交付分支上一个提交都没有。
    """
    base = _git(flaky_repo, "rev-parse", "HEAD").strip()
    state = await run_once(
        flaky_repo, AifixConfig(), run_id="cancel",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _FIX_PATCH})),
            _tool("apply_patch", json.dumps({"diff": _UNFIX_PATCH})),
            _text("已修复")]))

    assert state["results"][0]["verdict"] == "same"
    # 报告那一侧单独断言：只看 verdict 会漏掉「判定对了、报告仍在承诺」
    assert "已修复" not in state["report_md"]
    assert "修复：**0 / 1**" in state["report_md"]
    # 分支那一侧：`git merge` 承诺的东西必须真的存在
    assert _git(flaky_repo, "rev-parse", "aifix/cancel").strip() == base

    keys = _fact_keys(flaky_repo, "cancel")
    # 与 baseline_flaky 分开记：那条是「压根没碰」，这条是「碰了但抵消了」，
    # 复盘时要区分的是模型的行为，不是判定的结果
    assert "patch_cancelled_out" in keys


_NEW_FILE_TEST = '''def test_add():
    from calc import add

    assert add(2, 3) == 5
'''

_CREATE_PATCH = (
    "--- /dev/null\n"
    "+++ b/calc.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def add(a, b):\n"
    "+    return a + b\n"
)


@pytest.fixture
def missing_module_repo(tmp_path: Path) -> Path:
    """calc.py 还不存在：修复的唯一形态就是新建一个文件。

    函数内 import 让收集阶段照常通过，失败落在用例上 —— baseline 拿到的是
    一个正常的 test_id，而不是收集错误。
    """
    repo = tmp_path / "newfile"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_calc.py").write_text(
        _NEW_FILE_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _init_repo(repo)
    return repo


async def test_creating_a_new_file_still_counts_as_a_real_fix(
        missing_module_repo):
    """反向断言：新建文件是合法修复，判定仍是 better，提交必须真的产生。

    这一条防的是修过头。用 `git diff` 当「有没有改动」的判据会在这里翻车 ——
    它看不见未跟踪文件，于是一个真正修好了用例的补丁被降级成 same、回滚、
    连同报告一起丢掉。那比多报一次修复更糟。
    """
    base = _git(missing_module_repo, "rev-parse", "HEAD").strip()
    state = await run_once(
        missing_module_repo, AifixConfig(), run_id="newfile",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _CREATE_PATCH})),
            _text("已修复")]))

    assert state["results"][0]["verdict"] == "better"
    assert "已修复" in state["report_md"]
    assert "修复：**1 / 1**" in state["report_md"]
    # 分支上确实多了一个提交，且内容就是那个新文件
    assert _git(missing_module_repo, "rev-list", "--count",
                f"{base}..aifix/newfile").strip() == "1"
    assert "a + b" in _git(missing_module_repo, "show", "aifix/newfile:calc.py")
    assert "patch_cancelled_out" not in _fact_keys(missing_module_repo,
                                                   "newfile")
