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


async def test_replay_does_not_claim_attribution_is_missing(run_dir):
    """事件流现在自带 failure / attempt，回放不能再声明它不带。

    诊断工具描述自己的输入时说错话，比少印一行更糟：读的人会据此认定
    「事件与事实对不上」，转而去手工拼那条它本可以直接给出的时间轴。
    """
    out = render(run_dir)

    assert "不带 failure / attempt" not in out
    # 前提：这批产物确实带了归属（否则上面那条是靠「凑巧没这句话」通过的）
    events = [json.loads(x) for x in
              (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all("failure" in e for e in events)


def test_replay_reads_old_artifacts_without_attribution(tmp_path):
    """构造目录：M4 之前落下的 events.jsonl 没有这两个字段。

    渲染器读到缺字段不能崩，并且**要照旧说明**归属对不上 —— 老产物里
    事件与事实之间确实没有可靠的逐步对应关系。
    """
    d = tmp_path / "runs" / "旧产物"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        json.dumps({"type": "RunStarted", "data": {"run_id": "old"}}) + "\n"
        + json.dumps({"type": "StepStarted", "data": {"step": 1}}) + "\n",
        encoding="utf-8")
    (d / "facts.jsonl").write_text(
        json.dumps({"run_id": "old", "key": "verdict", "value": "better",
                    "failure": _TID, "attempt": 1}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    out = render(d)

    assert "Traceback" not in out
    assert "verdict" in out and _TID in out
    assert "不带 failure / attempt" in out


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


# —— 多 failure 的 run：归属必须真的被用起来 ——
#
# 上面每一条用例都只有一个 failure，于是「全部事实堆在整条时间轴之后」和
# 「事实插到对应位置」渲染出来一模一样 —— 一个 failure 的 run 分不出这两种
# 实现。计划与规格都写明另一种做法（把领域事实按其所属的 failure 与 attempt
# 插进对应位置），判据只能是多 failure 的 run。

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
# 「一直延伸到文件末尾」，git apply --check 直接判 patch does not apply。
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

_TID_ADD = "tests/test_calc.py::test_add"
_TID_SUB = "tests/test_calc.py::test_sub"


@pytest.fixture
def two_failure_run_dir(tmp_path) -> Path:
    repo = tmp_path / "two"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_TWO_BUGS, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TWO_TESTS, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("add", "-A"),
                 ("commit", "-q", "-m", "init")):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    return repo


async def _two_failure_run(repo: Path) -> Path:
    await run_once(
        repo, AifixConfig(max_attempts=1), run_id="two",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _FIX_ADD})),
            _text("已修复"),
            _tool("apply_patch", json.dumps({"diff": _FIX_SUB})),
            _text("已修复")]))
    return repo / ".aifix" / "runs" / "two"


async def test_every_step_says_which_failure_and_attempt_it_belongs_to(
        two_failure_run_dir):
    """一条无标记的扁平时间轴，读的人只能按位置猜「这是哪个用例的第几次尝试」。

    归属现在真的写在每条事件上了（RunTrace.record_events 并入 _current），
    渲染侧不用它，等于把它扔了。
    """
    out = render(await _two_failure_run(two_failure_run_dir), max_chars=60)

    headers = [ln for ln in out.splitlines() if ln.startswith("── 步骤")]
    assert len(headers) > 2, f"至少要有两个 failure 的步骤：{headers}"
    unmarked = [h for h in headers if _TID_ADD not in h and _TID_SUB not in h]
    assert not unmarked, f"这些步骤没写归属：{unmarked}"
    assert any(_TID_ADD in h for h in headers)
    assert any(_TID_SUB in h for h in headers)
    assert all("第 1 次尝试" in h for h in headers), headers


async def test_facts_are_inserted_where_they_belong_not_piled_up_at_the_end(
        two_failure_run_dir):
    """事实插到对应位置 —— 见计划 §237 与规格 §125。

    全堆在末尾时，第一个 failure 的 verdict 排在第二个 failure 的步骤之后，
    读的人要在两处之间来回翻才能把「这次尝试做了什么」和「判成了什么」对上。
    """
    out = render(await _two_failure_run(two_failure_run_dir), max_chars=60)

    headers = [ln for ln in out.splitlines() if ln.startswith("── 步骤")]
    first_sub_step = min(out.index(h) for h in headers if _TID_SUB in h)
    last_add_step = max(out.index(h) for h in headers if _TID_ADD in h)
    add_facts = out.index(f"── 事实 · {_TID_ADD}")

    assert last_add_step < add_facts < first_sub_step, (
        "test_add 的事实必须夹在它自己的步骤之后、test_sub 的步骤之前")
    # run 级事实仍在（中止原因就挂在那儿），且开头那批保持原序排在最前
    assert "baseline_failures" in out
    assert out.index("baseline_failures") < first_sub_step
