from aifix.config import AifixConfig
from aifix.graph import check_circuit_breaker, route_after_verify


def _st(consecutive: int, limit: int = 3, **over):
    return {"consecutive_failures": consecutive,
            "config": AifixConfig(consecutive_failure_limit=limit),
            "abort": None, "current": None, "queue": ["x"], **over}


def test_below_limit_does_not_trip():
    assert check_circuit_breaker(_st(2)) is None


def test_at_limit_trips():
    msg = check_circuit_breaker(_st(3))
    assert msg is not None
    assert "连续 3" in msg


def test_above_limit_trips():
    assert check_circuit_breaker(_st(5)) is not None


def test_success_resets_counter():
    """verify 判 BETTER 时把计数清零 —— 熔断看的是连续，不是累计。"""
    assert check_circuit_breaker(_st(0)) is None


def test_route_after_verify_goes_to_report_when_tripped():
    assert route_after_verify(_st(3)) == "report"


def test_route_after_verify_continues_when_not_tripped():
    assert route_after_verify(_st(1)) == "detect"


def test_limit_is_configurable():
    """阈值可调：接一个已知难修的项目时可以放宽。"""
    assert check_circuit_breaker(_st(3, limit=5)) is None
    assert check_circuit_breaker(_st(5, limit=5)) is not None
