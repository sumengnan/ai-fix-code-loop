"""信号从 verify 走到报告的接线。

这里验的不是 signals.py 的算法（那在 test_signals.py），而是三件接线上的事：
信号在 commit 之前算出来、有信号才写 fact、报告里能看见它 —— 且**判定完全
不受影响**。规格 §1 定的是「审查者是人」，信号只给人一个提示。
"""
import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig

# 与 test_e2e.py 里的脚本化模型替身逐字相同。本想直接 import 复用，但
# pytest 配了 --import-mode=importlib 且 tests/ 不是包，测试模块之间 import
# 不到（ModuleNotFoundError: No module named 'test_e2e'）。共用的去处只有
# conftest.py，而本任务不该动它 —— 于是照抄，不另造一套形状不同的替身。


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

_SRC = '''def add(a, b):
    return a - b        # bug: 应为 a + b


def mul(a, b):
    return a * b
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

# M3 真实验收里那种补丁：目标测试确实转绿，顺手删掉无测试覆盖的 mul，
# 又塞进一个模块级可变字典。每一道守卫都放行 —— 它们查的是行为不是合理性。
# 上下文的空行写成 " \n"（一个空格）而不是裸换行：unified diff 里上下文行
# 以空格开头，写成源码里的空行容易被编辑器/格式化工具去掉尾随空格而打不上。
_SUSPECT_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,6 +1,5 @@\n"
    "-def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+CACHE = {}\n"
    " \n"
    " \n"
    "-def mul(a, b):\n"
    "-    return a * b\n"
    "+def add(a, b):\n"
    "+    return a + b\n"
)

# 必须带上 mul 那几行尾部上下文：没有尾部上下文的 hunk 会被 git apply 当作
# 「一直改到文件末尾」（apply.c 的 match_end），在这个 6 行的文件上打不上。
_CLEAN_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,6 +1,6 @@\n"
    " def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+    return a + b\n"
    " \n"
    " \n"
    " def mul(a, b):\n"
    "     return a * b\n"
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def signals_repo(tmp_path: Path) -> Path:
    """比 buggy_repo 多一个 mul：它无测试覆盖，是「被顺手删掉」的那个符号。"""
    repo = tmp_path / "sig"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_SRC, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _fact_keys(repo: Path, run_id: str) -> list[str]:
    p = repo / ".aifix" / "runs" / run_id / "facts.jsonl"
    return [json.loads(ln)["key"] for ln in
            p.read_text(encoding="utf-8").splitlines() if ln.strip()]


async def _run(repo: Path, run_id: str, patch: str):
    return await run_once(
        repo, AifixConfig(), run_id=run_id,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": patch})),
                                _text("已修复")]))


async def test_verify_records_signals_and_report_shows_them(signals_repo):
    """删掉一个公开函数 + 新增模块级 dict，报告必须有「值得多看一眼」。"""
    state = await _run(signals_repo, "sig1", _SUSPECT_PATCH)

    assert "值得多看一眼" in state["report_md"]
    assert "mul" in state["report_md"]
    assert "CACHE" in state["report_md"]
    # 判定不受影响 —— 信号只标注
    assert state["results"][0]["verdict"] == "better"
    # 信号必须在 commit 之前算：交付分支上的补丁照常提交
    assert "a + b" in _git(signals_repo, "show", "aifix/sig1:calc.py")

    keys = _fact_keys(signals_repo, "sig1")
    assert "removed_public_symbol" in keys
    assert "new_module_state" in keys
    # 改动没出嫌疑文件，这一条就不该出现
    assert "files_outside_suspect" not in keys


async def test_clean_patch_report_has_no_signal_section(signals_repo):
    """正常修复的报告里不该出现这一节，否则它会被无视。"""
    state = await _run(signals_repo, "sig2", _CLEAN_PATCH)

    assert state["results"][0]["verdict"] == "better"
    assert "值得多看一眼" not in state["report_md"]
    assert "mul" not in state["report_md"]
    # 没信号时一条 fact 都不写：恒定出现的空 fact 会让 facts.jsonl 变噪音
    keys = _fact_keys(signals_repo, "sig2")
    assert "removed_public_symbol" not in keys
    assert "new_module_state" not in keys
    assert "files_outside_suspect" not in keys
