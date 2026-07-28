import pytest

from aifix.eval.task import Task, TaskResult, read_jsonl, write_jsonl

_T = Task(task_id="proj@abc1234::tests/test_calc.py::test_add",
          repo="/tmp/proj", commit="abc1234", base_commit="def5678",
          test_files=["tests/test_calc.py"],
          target_test="tests/test_calc.py::test_add",
          gold_files=["calc.py"])


def test_roundtrip(tmp_path):
    p = tmp_path / "tasks.jsonl"
    write_jsonl(p, [_T])
    back = read_jsonl(p, Task)
    assert back == [_T]


def test_creates_parent_directory(tmp_path):
    p = tmp_path / "deep" / "nested" / "tasks.jsonl"
    write_jsonl(p, [_T])
    assert p.is_file()


def test_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text(_T.model_dump_json() + "\n\n\n", encoding="utf-8")
    assert len(read_jsonl(p, Task)) == 1


def test_non_ascii_survives_roundtrip(tmp_path):
    """中文路径不能被转义成 \\uXXXX —— 任务集是要人看的。"""
    t = _T.model_copy(update={"gold_files": ["源码/计算.py"]})
    p = tmp_path / "tasks.jsonl"
    write_jsonl(p, [t])
    assert "源码/计算.py" in p.read_text(encoding="utf-8")
    assert read_jsonl(p, Task)[0].gold_files == ["源码/计算.py"]


def test_result_defaults():
    r = TaskResult(task_id="x", model="m", locate_hit=False, suspect_file=None,
                   verdict="same", attempts=1, tokens=10, cost_usd=0.1,
                   violations=0)
    assert r.abort_reason is None
    assert r.error is None


def test_adapter_defaults_to_pytest():
    assert _T.adapter == "pytest"


def test_missing_required_field_rejected():
    with pytest.raises(Exception):
        Task(task_id="x")
