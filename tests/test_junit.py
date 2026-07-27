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


def test_missing_file_is_ignored(tmp_path):
    fs = parse_junit([tmp_path / "nope.xml"], lambda c, n, f: f"{c}::{n}")
    assert fs.ids == set()
