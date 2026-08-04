"""必要性反查从 verify 走到报告的接线。

验的不是 necessity.py 的算法（那在 test_necessity.py），而是四件接线上的事：
反查在 commit **之前**跑、查出来的东西**照样交付**（只出信号不改交付）、
报告里能看见它、以及**判定完全不受影响**。
"""
import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

# 与 test_signals_wiring.py / test_e2e.py 里的模型替身逐字相同。pytest 配了
# --import-mode=importlib 且 tests/ 不是包，测试模块之间 import 不到 —— 共用
# 的去处只有 conftest.py，而那里不是放模型替身的地方。


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


# add 与 helper 之间隔了 8 行：git diff 默认带 3 行上下文，挨得近的两处改动
# 会被并成**一个** hunk，那样就问不出「逐个反向」这件事了。
_SRC = '''def add(a, b):
    return a - b        # bug: 应为 a + b


def mul(a, b):
    return a * b


def div(a, b):
    return a / b


def helper():
    return 1
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5
'''

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})

# 两个 hunk：一个真修了 bug，一个改了与目标用例毫无关系的 helper。
# 三条静态信号一条都不亮 —— 没删公开符号、没加模块级状态、改的就是嫌疑文件。
# 这正是这一层要抓的形状。
_MIXED_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,5 +1,5 @@\n"
    " def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+    return a + b\n"
    " \n"
    " \n"
    " def mul(a, b):\n"
    "@@ -10,4 +10,4 @@ def div(a, b):\n"
    " \n"
    " \n"
    " def helper():\n"
    "-    return 1\n"
    "+    return 2\n"
)

# 只改该改的地方，一个 hunk。反查会因为「只有一个单位」直接跳过。
_CLEAN_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,5 +1,5 @@\n"
    " def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+    return a + b\n"
    " \n"
    " \n"
    " def mul(a, b):\n"
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "nec"
    (r / "tests").mkdir(parents=True)
    (r / "calc.py").write_text(_SRC, encoding="utf-8")
    (r / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _fact_keys(repo: Path, run_id: str) -> list[str]:
    p = repo / ".aifix" / "runs" / run_id / "facts.jsonl"
    return [json.loads(ln)["key"] for ln in
            p.read_text(encoding="utf-8").splitlines() if ln.strip()]


async def _run(repo: Path, run_id: str, patch: str, **cfg):
    return await run_once(
        repo, AifixConfig(**cfg), run_id=run_id,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": patch})),
                                _text("已修复")]))


async def test_unnecessary_hunk_is_reported_but_still_delivered(repo):
    state = await _run(repo, "nec1", _MIXED_PATCH)

    # 判定不受影响
    assert state["results"][0]["verdict"] == "better"
    assert "unnecessary_hunk" in _fact_keys(repo, "nec1")

    # 报告里看得见，且带着那条误报注脚
    assert "值得多看一眼" in state["report_md"]
    assert "对修复没有贡献" in state["report_md"]
    assert "没有跑全量" in state["report_md"]

    # **交付内容一个字都没变**：查出来的多余改动照样在交付分支上。
    # 这一条是这一层的边界 —— 它只出信号，不替人做删改动的决定。
    delivered = _git(repo, "show", "aifix/nec1:calc.py")
    assert "return a + b" in delivered
    assert "return 2" in delivered          # 多余的那处改动仍在


async def test_clean_single_hunk_patch_says_nothing(repo):
    """只有一个单位时整层跳过，报告里不该有这一节。"""
    state = await _run(repo, "nec2", _CLEAN_PATCH)

    assert state["results"][0]["verdict"] == "better"
    assert "unnecessary_hunk" not in _fact_keys(repo, "nec2")
    assert "值得多看一眼" not in state["report_md"]


async def test_switch_off_skips_the_whole_layer(repo):
    """关掉之后一条 fact 都不写，判定与交付照旧。"""
    state = await _run(repo, "nec3", _MIXED_PATCH, necessity_check=False)

    assert state["results"][0]["verdict"] == "better"
    assert "unnecessary_hunk" not in _fact_keys(repo, "nec3")
    assert "return 2" in _git(repo, "show", "aifix/nec3:calc.py")


async def test_over_the_cap_says_so_instead_of_going_quiet(repo):
    """上限设成 1 时整体跳过 —— 但必须说出来，不能装作查过了。

    「补丁太大所以没查」和「查了，很干净」在报告里长得一样，是这一层最危险
    的失效方式：人会把沉默读成背书。
    """
    state = await _run(repo, "nec4", _MIXED_PATCH, necessity_max_units=1)

    assert state["results"][0]["verdict"] == "better"
    keys = _fact_keys(repo, "nec4")
    assert "unnecessary_hunk" not in keys        # 确实没查出东西
    assert "necessity_over_cap" in keys          # 但也确实没查
    assert "整层没有跑" in state["report_md"]
    assert "没被报出来不等于都必要" in state["report_md"]
    # 判定与交付照旧
    assert "return a + b" in _git(repo, "show", "aifix/nec4:calc.py")


def test_report_shows_the_changed_lines_not_just_a_location():
    """报告里要给那几行改了什么。

    只给 `calc.py:10-13` 的话，人拿到报告的下一步必然是打开 diff 去数行 ——
    而这一节存在的意义就是让「值不值得细看」这个判断在报告里就能做完。
    """
    from aifix.nodes.report import _signal_section

    md = "\n".join(_signal_section([{
        "test_id": "t::x",
        "removed_public_symbols": [], "new_module_state": [],
        "files_outside_suspect": [], "hardcoded_literals": [],
        "unnecessary_hunks": [{"label": "calc.py:10-13",
                               "preview": "-    return 1\n+    return 2"}],
        "necessity_skipped": [], "necessity_over_cap": 0,
    }]))

    assert "`calc.py:10-13`" in md
    assert "+    return 2" in md
    assert "```diff" in md


def test_report_still_renders_labels_from_an_old_checkpoint():
    """旧 checkpoint 里这个 key 存的是一串裸标签，不能在读报告时炸掉。

    炸掉的时机最坏：修复早已提交进交付分支，用户拿到的是一个「全都做完了却在
    最后一步崩了」的 run。
    """
    from aifix.nodes.report import _signal_section

    md = "\n".join(_signal_section([{
        "test_id": "t::x",
        "removed_public_symbols": [], "new_module_state": [],
        "files_outside_suspect": [], "hardcoded_literals": [],
        "unnecessary_hunks": ["calc.py:10-13"],
    }]))

    assert "`calc.py:10-13`" in md
