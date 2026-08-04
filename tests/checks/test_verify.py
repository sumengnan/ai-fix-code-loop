import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.adapters.base import Failure, FailureSet, Verdict
from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.checks.verify import compare


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


# —— 交付失败：git add 退非 0 时，这次 run 已经交付的成果不许失联 ——
#
# `git add -- <路径列表>` 里只要有一条匹配不到，git 退 128 且**一条都不暂存**；
# 新文件命中 .gitignore 时退 1，而此时别的路径**已经**暂存了。两种形态实测
# 确认过。Worktree.commit 对此抛 RuntimeError —— 抛得对，但一路裸穿到
# _cmd_run：worktree 被删、report_node 根本执行不到，用户拿到一段调用栈，
# report.md 不存在，本次 run 前面几个 failure 已经提交进交付分支的修复也
# 没人告诉他。

_TWO_BUGS = '''def add(a, b):
    return a - b


def sub(a, b):
    return a + b
'''

_TWO_TESTS = '''from calc import add, sub


def test_add():
    assert add(2, 3) == 5


def test_sub():
    assert sub(5, 3) == 2
'''

# 尾部留一行上下文：文件后面还有内容时，不带尾部上下文的 hunk 会被 git 当成
# 「一直延伸到文件末尾」，`git apply --check` 直接判 patch does not apply。
_FIX_ADD = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
    " \n"
)

_FIX_SUB = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -4,3 +4,3 @@\n"
    " \n"
    " def sub(a, b):\n"
    "-    return a + b\n"
    "+    return a - b\n"
)

# 从未被跟踪过的新文件，随后又被删掉：touched 里留着它，工作区里没有它
_MAKE_HELPER = (
    "--- /dev/null\n"
    "+++ b/helper.py\n"
    "@@ -0,0 +1 @@\n"
    "+MARK = 1\n"
)

_DROP_HELPER = (
    "--- a/helper.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-MARK = 1\n"
)


@pytest.fixture
def two_bug_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "two"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_TWO_BUGS, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TWO_TESTS, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _init_repo(repo)
    return repo


async def test_a_dangling_pathspec_does_not_lose_what_was_already_delivered(
        two_bug_repo):
    """第一个 failure 修好并提交，第二个交付失败 —— 报告必须照常产出。

    模型先新建一个从未被跟踪的 helper.py，改完 calc.py 后又发一个把它删掉的
    补丁：touched 里两条都在，工作区里只剩一条，`git add -- calc.py helper.py`
    退 128 且一条都不暂存（实测）。
    """
    base = _git(two_bug_repo, "rev-parse", "HEAD").strip()
    state = await run_once(
        two_bug_repo, AifixConfig(max_attempts=1), run_id="dangling",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _FIX_ADD})),
            _text("已修复"),
            _tool("apply_patch", json.dumps({"diff": _MAKE_HELPER})),
            _tool("apply_patch", json.dumps({"diff": _FIX_SUB})),
            _tool("apply_patch", json.dumps({"diff": _DROP_HELPER})),
            _text("已修复")]))

    # 1. 报告必须存在 —— 内存里一份、盘上一份。这是用户唯一的成果凭据。
    assert "修复：" in state["report_md"]
    on_disk = (two_bug_repo / ".aifix" / "runs" / "dangling"
               / "report.md").read_text(encoding="utf-8")
    assert on_disk == state["report_md"]

    # 2. 前一个 failure 交付到分支上的提交还在，且报告里说得出来
    assert _git(two_bug_repo, "rev-list", "--count",
                f"{base}..aifix/dangling").strip() == "1"
    assert "修复：**1 / 2**" in state["report_md"]
    verdicts = {r["test_id"]: r["verdict"] for r in state["results"]}
    assert verdicts["tests/test_calc.py::test_add"] == "better"

    # 3. 交付失败的那一个不许算成已修复，且报告里看得出出了什么事
    assert verdicts["tests/test_calc.py::test_sub"] != "better"
    assert "交付失败" in state["report_md"], state["report_md"]
    assert "delivery_failed" in _fact_keys(two_bug_repo, "dangling")


@pytest.fixture
def ignored_new_file_repo(tmp_path: Path) -> Path:
    """.gitignore 盖住 build/：模型往那儿新建文件，git add 退 1。"""
    repo = tmp_path / "ignored"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n',
        encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _init_repo(repo)
    return repo


_MAKE_IGNORED = (
    "--- /dev/null\n"
    "+++ b/build/helper.py\n"
    "@@ -0,0 +1 @@\n"
    "+MARK = 1\n"
)


async def test_an_ignored_new_file_does_not_kill_the_report(
        ignored_new_file_repo):
    """第二种形态：新文件命中 .gitignore，git add 退 1，而 calc.py 已被暂存。

    半个暂存区留在原地也是个坑：rollback 只 checkout 工作区，被暂存的内容
    会被从索引里还原回工作区，泄漏进下一个 failure 的尝试。
    """
    base = _git(ignored_new_file_repo, "rev-parse", "HEAD").strip()
    state = await run_once(
        ignored_new_file_repo, AifixConfig(max_attempts=1), run_id="ignored",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _MAKE_IGNORED})),
            _tool("apply_patch", json.dumps({"diff": _FIX_PATCH})),
            _text("已修复")]))

    assert "修复：**0 / 1**" in state["report_md"]
    assert "交付失败" in state["report_md"], state["report_md"]
    assert state["results"][0]["verdict"] != "better"
    # 交付分支上什么都没有 —— 半个暂存区不许变成半个提交
    assert _git(ignored_new_file_repo, "rev-parse",
                "aifix/ignored").strip() == base
    assert "delivery_failed" in _fact_keys(ignored_new_file_repo, "ignored")


async def test_an_unexpected_crash_still_produces_a_report(
        two_bug_repo, monkeypatch):
    """兜底那一层：核心循环里任何一个节点炸掉，报告仍然要产出。

    verify_node 那一层接住的是**已知**的交付失败形态。这一条钉的是别的：
    worktree 在 `with` 退出时就被删了，报告是用户手里唯一的成果凭据 ——
    异常裸穿出去等于让这次 run 已经交付到分支上的东西整个失联。
    """
    from aifix import cli as cli_mod

    # `**_` 不能省：`verify_node` 现在还收一个 `reviewer_client`（测试注入口，
    # 与 detect / fix 两个节点同款）。签名对不上的话炸出来的是 TypeError 而不是
    # 这里要模拟的那个 RuntimeError —— 报告照样产出，于是这条测试**看起来**还是
    # 绿的，实际钉的已经不是它要钉的东西了。
    async def _boom(state, **_):
        raise RuntimeError("模拟节点崩溃")

    monkeypatch.setattr(cli_mod, "verify_node", _boom)
    state = await run_once(
        two_bug_repo, AifixConfig(max_attempts=1), run_id="boom",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _FIX_ADD})),
            _text("已修复")]))

    assert state["abort_kind"] == "crash"
    assert "模拟节点崩溃" in state["report_md"], state["report_md"]
    on_disk = (two_bug_repo / ".aifix" / "runs" / "boom"
               / "report.md").read_text(encoding="utf-8")
    assert on_disk == state["report_md"]
    assert "crash" in _fact_keys(two_bug_repo, "boom")
