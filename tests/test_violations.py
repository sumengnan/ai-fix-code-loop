import inspect

import pytest
from harness.events import RunError, TextDelta, ToolFinished, ToolStarted
from harness.types import ToolCall, ToolResult

from aifix.violations import _LOOP, _PATH_ESCAPE, _TEST_EDIT, count_violations


def _started(call_id, name):
    return ToolStarted(tool_call=ToolCall(id=call_id, name=name, arguments={}))


def _finished(call_id, content, is_error=True):
    return ToolFinished(result=ToolResult(call_id, content, is_error=is_error))


def test_counts_test_file_edit_attempt():
    evs = [_started("1", "apply_patch"),
           _finished("1", "拒绝修改测试文件：tests/test_x.py。请修改源码……")]
    assert count_violations(evs)["test_edit"] == 1


def test_counts_path_escape_attempt():
    evs = [_started("1", "apply_patch"),
           _finished("1", "路径逃逸工作区：../../evil.py")]
    assert count_violations(evs)["path_escape"] == 1


def test_counts_loop_abort():
    evs = [RunError(error="检测到疑似循环：纠偏后仍连续重复相同的工具调用"
                          "（apply_patch），已中止")]
    assert count_violations(evs)["loop_abort"] == 1


def test_ordinary_patch_failure_is_not_a_violation():
    """补丁打不上是模型能力问题，不是越界 —— 混进来会让这列失去意义。"""
    evs = [_started("1", "apply_patch"),
           _finished("1", "补丁无法应用（git apply --check 失败）：……")]
    assert count_violations(evs) == {"test_edit": 0, "path_escape": 0,
                                     "loop_abort": 0}


def test_errors_from_other_tools_are_ignored():
    """只有 apply_patch 能越界改文件；别的工具报错不算。"""
    evs = [_started("1", "read_file"),
           _finished("1", "路径逃逸工作区：../../etc/passwd")]
    assert count_violations(evs)["path_escape"] == 0


def test_successful_calls_are_not_counted():
    evs = [_started("1", "apply_patch"),
           _finished("1", "补丁已应用。", is_error=False)]
    assert sum(count_violations(evs).values()) == 0


def test_non_loop_run_error_is_ignored():
    evs = [RunError(error="模型调用失败: 连接超时")]
    assert count_violations(evs)["loop_abort"] == 0


def test_counts_accumulate():
    evs = [_started("1", "apply_patch"),
           _finished("1", "拒绝修改测试文件：tests/a.py"),
           TextDelta(text="换一个"),
           _started("2", "apply_patch"),
           _finished("2", "拒绝修改测试文件：tests/b.py")]
    assert count_violations(evs)["test_edit"] == 2


def test_empty_stream():
    assert count_violations([]) == {"test_edit": 0, "path_escape": 0,
                                    "loop_abort": 0}


# ---------- 哨兵：两条匹配串来自第三方依赖 ----------
#
# 上面所有用例都自己构造字符串，所以上游把措辞一改，它们照样全绿，而
# path_escape / loop_abort 两列会永久归零 —— 不报错，只是数字从此是假的。
# pyproject 里 ai-harness-framework 只写了 >=0.0.2，没锁上界，这件事随时
# 可能发生。下面两个测试直接对着上游的真实产物断言。


def test_sentinel_path_escape_wording_matches_upstream(tmp_path):
    """哨兵：`路径逃逸` 由 harness/sandbox/base.py（第三方依赖）产生。"""
    from harness.sandbox.base import SandboxError, resolve_in_workspace

    with pytest.raises(SandboxError) as e:
        resolve_in_workspace(str(tmp_path), "../../evil.py")
    assert _PATH_ESCAPE in str(e.value), (
        "上游改了路径逃逸的措辞：violations 的 path_escape 一列已经恒为 0，"
        "请同步 _PATH_ESCAPE 并考虑给 ai-harness-framework 锁上界")


def test_sentinel_loop_abort_wording_matches_upstream():
    """哨兵：`检测到疑似循环` 由 harness/loop/agent_loop.py（第三方依赖）产生。

    这里读源码而不是真触发一次循环中止：真触发要跑满一整轮 AgentLoop 的
    重复调用检测，代价远高于这条断言的价值。折中的代价是「串还在源码里」
    不等于「串还在那条 RunError 上」，但上游改措辞时源码必然一起改，
    这条哨兵仍然能第一时间报警。
    """
    import harness.loop.agent_loop as agent_loop

    assert _LOOP in inspect.getsource(agent_loop), (
        "上游改了循环中止的措辞：violations 的 loop_abort 一列已经恒为 0，"
        "请同步 _LOOP 并考虑给 ai-harness-framework 锁上界")


def test_sentinel_test_edit_wording_matches_our_own_tool():
    """对照组：这一条由本仓库的 patch.py 产生，改动在我们自己手上。"""
    from aifix.tools import patch

    assert _TEST_EDIT in inspect.getsource(patch)
