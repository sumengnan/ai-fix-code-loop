from aifix.adapters.base import Failure, FailureSet, SourceCandidate, Verdict


def _f(tid: str) -> Failure:
    return Failure(test_id=tid, classname="c", name="n", message="m", trace="t")


def test_failure_set_ids():
    fs = FailureSet({"a": _f("a"), "b": _f("b")})
    assert fs.ids == {"a", "b"}


def test_failure_set_empty():
    assert FailureSet({}).ids == set()
    assert not FailureSet({}).failures


def test_verdict_values():
    assert Verdict.BETTER.value == "better"
    assert Verdict.SAME.value == "same"
    assert Verdict.WORSE.value == "worse"


def test_source_candidate_fields():
    sc = SourceCandidate(path="calc.py", line=2, frame="add")
    assert (sc.path, sc.line, sc.frame) == ("calc.py", 2, "add")
