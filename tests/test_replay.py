"""`aifix replay` 的渲染核心。

这里的每一份 events.jsonl 都是**真跑出来**的：用脚本化模型替身驱动一次
完整的 run_once，再把落盘目录喂给 render。手写事件字典只能证明我们对
格式的理解自洽，证明不了 `event_to_dict` 真的是那么写的。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.replay import render

_TID = "tests/test_calc.py::test_add"

_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""

# ensure_ascii=False：真模型吐的是 UTF-8 原文，不是 减号 这种转义。
# 用默认的 ensure_ascii 会让下面「某段文本不在输出里」的断言恒真 —— 那种
# 断言插错列都发现不了。
_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
}, ensure_ascii=False)


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


@pytest.fixture
async def run_dir(buggy_repo) -> Path:
    """真跑一次 run_once，返回 .aifix/runs/<id>/。"""
    await run_once(buggy_repo, AifixConfig(), run_id="rp",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch", json.dumps({"diff": _PATCH})),
                       _text("已修复")]))
    d = buggy_repo / ".aifix" / "runs" / "rp"
    assert (d / "events.jsonl").exists(), "前提没成立：这次 run 没落下事件"
    return d


async def test_replay_shows_every_tool_call_and_result(run_dir):
    out = render(run_dir)

    # 1. 工具调用的名字与参数：光有名字不够，参数错了照样看不出来
    assert "apply_patch" in out
    assert "return a + b" in out, "补丁参数的正文必须出现，否则复盘看不到改了什么"
    # 2. 工具**返回**了什么 —— 与调用同等重要，这一侧最常被漏渲染
    assert "补丁已应用" in out
    # 3. 模型说了什么（两个 agent 各一句）
    assert "减号应为加号" in out
    assert "已修复" in out
    # 4. token 与成本：没配价格表时不许显示假的 $0.00
    assert "15" in out
    assert "$0.00" not in out
    assert "未配置 AIFIX_PRICE_MAP" in out
    # 5. 领域事实：verdict 出现在写明归属的位置上
    verdict_block = [b for b in out.split("──") if "verdict" in b]
    assert verdict_block, "facts 里的 verdict 没被渲染"
    assert _TID in out and "better" in out
    idx_head = out.index(_TID)
    assert idx_head < out.index("verdict"), "verdict 必须落在写明 failure 的标题之后"
    # 6. run 级事实（没有 failure/attempt 字段）也要出现，不能只渲染挂在 attempt 上的
    assert "baseline_failures" in out


async def test_step_selects_exactly_one_step(run_dir):
    out_all = render(run_dir)
    out_one = render(run_dir, step=2)

    assert out_one.count("步骤") == 1
    assert len(out_one) < len(out_all)
    # 上面两条单独都不够：render 对未知 step 返回空串时它们照样通过。
    # 必须同时断言选中的那一步的**内容确实在**。
    assert "apply_patch" in out_one
    assert "补丁已应用" in out_one
    # 而别的步骤的内容不在
    assert "减号应为加号" not in out_one


async def test_truncation_is_marked_and_full_disables_it(run_dir):
    """截断必须**看得出来**被截断了 —— 悄悄截断是这个项目最忌讳的形状。"""
    trunc = render(run_dir, max_chars=50)
    full = render(run_dir, full=True)

    assert "已截断" in trunc
    # 标记之外还要真的截掉：补丁尾巴（原文第 110 字符开外）不该出现
    assert "return a + b" not in trunc
    # full=True 时原文完整，且不留任何截断标记
    assert "return a + b" in full
    assert "已截断" not in full


async def test_missing_run_dir_says_so_in_plain_words(tmp_path):
    """诊断工具的第一要务是让人找得到东西，不是抛 traceback。"""
    out = render(tmp_path / "runs" / "不存在")

    assert "不存在" in out
    assert "Traceback" not in out
    # 人话：得说清楚该去哪儿找
    assert ".aifix/runs" in out


async def test_run_dir_without_events_is_reported_not_crashed(tmp_path):
    d = tmp_path / "runs" / "半截"
    d.mkdir(parents=True)
    (d / "facts.jsonl").write_text(
        json.dumps({"run_id": "半截", "key": "abort", "value": "预算耗尽"},
                   ensure_ascii=False) + "\n", encoding="utf-8")

    out = render(d)

    assert "events.jsonl" in out
    # 事件没了，但已有的事实还得给人看 —— 那可能正是他要找的东西
    assert "预算耗尽" in out


async def test_unknown_step_number_is_reported(run_dir):
    out = render(run_dir, step=999)

    assert "999" in out
    assert "Traceback" not in out
    assert "apply_patch" not in out


def test_repo_stays_clean(buggy_repo):
    """回放是只读的 —— 顺手守住这条，别让渲染器哪天开始写文件。"""
    before = subprocess.run(["git", "status", "--porcelain"], cwd=buggy_repo,
                            capture_output=True, text=True).stdout
    render(buggy_repo / ".aifix" / "runs" / "无此 run")
    after = subprocess.run(["git", "status", "--porcelain"], cwd=buggy_repo,
                           capture_output=True, text=True).stdout
    assert before == after
