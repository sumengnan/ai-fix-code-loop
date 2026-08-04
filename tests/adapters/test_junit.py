from pathlib import Path

from aifix.adapters.junit import parse_junit

_XML = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" errors="1" skipped="0">
    <testcase classname="tests.test_calc" name="test_add" file="tests/test_calc.py" line="4" time="0.01">
      <failure message="assert -1 == 5">Traceback...\nE  assert -1 == 5</failure>
    </testcase>
    <testcase classname="tests.test_calc" name="test_boom" file="tests/test_calc.py" line="9" time="0.01">
      <error message="ZeroDivisionError">Traceback...\nZeroDivisionError</error>
    </testcase>
    <testcase classname="tests.test_calc" name="test_identity" file="tests/test_calc.py" line="8" time="0.01"/>
  </testsuite>
</testsuites>
'''


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "report.xml"
    p.write_text(_XML, encoding="utf-8")
    return p


def test_collects_failures_and_errors(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    assert fs.ids == {"tests.test_calc::test_add", "tests.test_calc::test_boom"}


def test_passing_case_excluded(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    assert "tests.test_calc::test_identity" not in fs.ids


def test_message_and_trace_captured(tmp_path):
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    fail = fs.failures["tests.test_calc::test_add"]
    assert fail.message == "assert -1 == 5"
    assert "assert -1 == 5" in fail.trace
    assert fail.file == "tests/test_calc.py"
    assert fail.line == 4


def test_multiple_report_files_merged(tmp_path):
    a = tmp_path / "a.xml"
    a.write_text(_XML, encoding="utf-8")
    b = tmp_path / "b.xml"
    b.write_text(_XML.replace("test_add", "test_other"), encoding="utf-8")
    fs = parse_junit([a, b], lambda c, n, f: f"{c}::{n}")
    assert "tests.test_calc::test_other" in fs.ids
    assert len(fs.ids) == 3


_XML_SKIP = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="1">
    <testcase classname="tests.test_calc" name="test_identity" file="tests/test_calc.py" line="8" time="0.01"/>
    <testcase classname="tests.test_calc" name="test_skipped" file="tests/test_calc.py" line="12" time="0.0">
      <skipped message="要 numpy" type="pytest.skip"/>
    </testcase>
  </testsuite>
</testsuites>
'''


def test_ran_covers_passed_and_failed(tmp_path):
    """挖任务要区分「跑了但通过」与「压根没跑」：后者不是红转绿。"""
    fs = parse_junit([_write(tmp_path)], lambda c, n, f: f"{c}::{n}")
    assert fs.ran == {"tests.test_calc::test_add", "tests.test_calc::test_boom",
                      "tests.test_calc::test_identity"}


def test_ran_excludes_skipped(tmp_path):
    """被跳过的用例既不失败也不算跑过 —— 红转跳过不是红转绿。"""
    p = tmp_path / "skip.xml"
    p.write_text(_XML_SKIP, encoding="utf-8")
    fs = parse_junit([p], lambda c, n, f: f"{c}::{n}")
    assert fs.ran == {"tests.test_calc::test_identity"}
    assert fs.ids == set()


def test_missing_file_is_ignored(tmp_path):
    fs = parse_junit([tmp_path / "nope.xml"], lambda c, n, f: f"{c}::{n}")
    assert fs.ids == set()
    assert fs.ran == set()


# ------------------------------------------------------------ 色码

# pytest 的 junitxml 把 XML 1.0 不允许的控制字符转义成**字面文本**：`\x1b`
# 落到报告里是七个可见字符 `#x1B`。下面这段是从真报告里原样抄来的（2026-08-03
# 实跑，FORCE_COLOR 打开时），不是手写的猜想。
_XML_COLOR = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="1" failures="1" errors="0" skipped="0">
    <testcase classname="tests.test_probe" name="test_x" file="tests/test_probe.py" line="9" time="0.01">
      <failure message="NameError: name 'undefined_thing' is not defined">#x1B[1m#x1B[31mtests/test_probe.py#x1B[0m:12: 
#x1B[1m#x1B[31mcalc.py#x1B[0m:5: NameError</failure>
    </testcase>
  </testsuite>
</testsuites>
'''


def test_pytests_escaped_color_codes_are_stripped(tmp_path):
    """色码必须在解析入口去掉，否则栈帧解析会**静默**失效。

    `_PYTEST_FRAME` 会把 `#x1B[1m#x1B[31mcalc.py#x1B[0m:5:` 的路径截成
    `#x1B[1m#x1B[31mcalc.py#x1B[0m`，`_resolve` 做 is_file() 落空，于是整条
    traceback 一帧都定位不到 —— Detector 收到「未能从栈帧定位」然后盲猜路径。
    不报错、不崩溃，只有定位悄悄变成了瞎猜。

    断言的是**字面量 `#x1B` 不再出现**，而不是「去掉了 ANSI」：匹配 `\\x1b`
    的正则对这串字面文本完全无效，而它看起来又像是已经处理过了。
    """
    p = tmp_path / "color.xml"
    p.write_text(_XML_COLOR, encoding="utf-8")
    fs = parse_junit([p], lambda c, n, f: f"{c}::{n}")
    fail = fs.failures["tests.test_probe::test_x"]
    assert "#x1B" not in fail.trace
    assert "\x1b" not in fail.trace
    # 路径必须完好地留下来 —— 只去色，不吃内容
    assert "calc.py:5: NameError" in fail.trace
    assert "tests/test_probe.py:12:" in fail.trace


def test_stripping_color_keeps_frames_locatable(tmp_path):
    """端到端：带色的报告要能定位到产品文件那一帧。

    上一条钉的是「字符没了」，这条钉的是「**因此**定位回来了」——两者分开，
    因为去色的正则可以在把 `#x1B` 删干净的同时把路径也吃掉一截，那样第一条
    仍然全绿。
    """
    from aifix.adapters.pytest_adapter import PytestAdapter

    (tmp_path / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_probe.py").write_text("y = 1\n", encoding="utf-8")
    p = tmp_path / "color.xml"
    p.write_text(_XML_COLOR, encoding="utf-8")

    a = PytestAdapter()
    fs = parse_junit([p], a.make_test_id)
    fail = next(iter(fs.failures.values()))
    frames = [(c.path, c.line) for c in a.locate_source(fail, tmp_path)
              if c.origin == "traceback"]
    assert ("calc.py", 5) in frames, frames
