import json

from harness.events import RunStarted, TextDelta

from aifix.trace import RunTrace


def test_events_written_as_jsonl(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.record_events([RunStarted(run_id="r1"), TextDelta(text="你好")])
    t.close()
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["type"] == "RunStarted"
    assert first["data"]["run_id"] == "r1"


def test_records_domain_facts(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.fact("verdict", "better", failure="a", attempt=1)
    t.close()
    lines = (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert rec["key"] == "verdict"
    assert rec["value"] == "better"
    assert rec["failure"] == "a"
    assert rec["attempt"] == 1


def test_span_context_lands_on_facts(tmp_path):
    """facts 要自带层级坐标，否则评测拿到一堆孤立结论无从归属。"""
    t = RunTrace(tmp_path, run_id="r1")
    with t.run_span():
        with t.failure_span("tests/x.py::test_y"):
            with t.attempt_span(2):
                t.fact("verdict", "same")
    t.fact("outside", 1)
    t.close()
    recs = [json.loads(x) for x in
            (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recs[0]["failure"] == "tests/x.py::test_y"
    assert recs[0]["attempt"] == 2
    assert "failure" not in recs[1], "退出 span 后不该再带上层级坐标"


def test_spans_nest_without_provider(tmp_path):
    """没配 OTel provider 时是 no-op tracer，不该报错。"""
    t = RunTrace(tmp_path, run_id="r1")
    with t.run_span():
        with t.failure_span("tests/x.py::test_y"):
            with t.attempt_span(1):
                t.fact("verdict", "same")
    t.close()
    assert (tmp_path / "facts.jsonl").exists()


def test_non_scalar_fact_is_serialized(tmp_path):
    """list/dict 也要能记 —— flaky_filtered 就是个列表。"""
    t = RunTrace(tmp_path, run_id="r1")
    t.fact("flaky_filtered", ["a", "b"])
    t.close()
    rec = json.loads(
        (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["value"] == ["a", "b"]


def test_close_is_idempotent(tmp_path):
    t = RunTrace(tmp_path, run_id="r1")
    t.close()
    t.close()


def test_creates_directory(tmp_path):
    d = tmp_path / "deep" / "nested"
    t = RunTrace(d, run_id="r1")
    t.fact("x", 1)
    t.close()
    assert (d / "facts.jsonl").is_file()
