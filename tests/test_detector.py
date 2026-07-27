import json

from aifix.adapters.base import Failure, SourceCandidate
from aifix.agents.detector import Diagnosis, build_prompt, parse_diagnosis

_FAILURE = Failure(
    test_id="tests/test_calc.py::test_add", classname="tests.test_calc",
    name="test_add", message="assert -1 == 5", trace="File ...\nE assert -1 == 5")
_CANDS = [SourceCandidate(path="calc.py", line=2, frame="add")]


def test_prompt_contains_test_id_message_and_candidates():
    p = build_prompt(_FAILURE, _CANDS)
    assert "tests/test_calc.py::test_add" in p
    assert "assert -1 == 5" in p
    assert "calc.py:2" in p


def test_prompt_handles_empty_candidates():
    p = build_prompt(_FAILURE, [])
    assert "（未能从栈帧定位到 repo 内的源码）" in p


def test_parse_valid_json():
    raw = json.dumps({
        "suspect_file": "calc.py", "suspect_lines": [1, 3],
        "root_cause": "减号写成了加号", "fix_strategy": "改回 a + b",
        "confidence": "high",
    })
    d = parse_diagnosis(raw)
    assert isinstance(d, Diagnosis)
    assert d.suspect_file == "calc.py"
    assert d.suspect_lines == (1, 3)
    assert d.confidence == "high"


def test_parse_tolerates_missing_optional_lines():
    raw = json.dumps({
        "suspect_file": "calc.py", "root_cause": "x",
        "fix_strategy": "y", "confidence": "low",
    })
    assert parse_diagnosis(raw).suspect_lines is None


def test_parse_returns_none_on_garbage():
    """解析失败是降级信号，不是异常 —— 上层改为把原始 traceback 交给 Fixer。"""
    assert parse_diagnosis("这不是 JSON") is None


def test_parse_returns_none_on_schema_mismatch():
    assert parse_diagnosis(json.dumps({"suspect_file": "calc.py"})) is None
