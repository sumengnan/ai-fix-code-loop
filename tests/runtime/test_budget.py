from aifix.runtime.budget import RunBudget


def test_allocates_by_remaining_failures():
    """动态分配：前面省下的额度自动流给后面难的。"""
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    assert b.for_failure(remaining_failures=4) == 25_000
    b.charge(tokens=5_000, cny=0.1)
    # 剩 95k，还剩 3 个 → 每个 31666
    assert b.for_failure(remaining_failures=3) == 31_666


def test_fixed_split_would_waste_but_dynamic_does_not():
    """固定切分会让最后一个 failure 明明有钱却因自己那份用完而放弃。"""
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=1_000, cny=0.01)     # 第一个只花了一点
    assert b.for_failure(remaining_failures=1) == 99_000


def test_never_returns_below_floor():
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=99_999, cny=0.0)
    assert b.for_failure(remaining_failures=1) == RunBudget.FLOOR_TOKENS


def test_zero_remaining_failures_returns_all():
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    assert b.for_failure(remaining_failures=0) == 100_000


def test_exhausted_on_tokens():
    b = RunBudget(total_tokens=1_000, total_cny=2.0, total_seconds=600)
    assert b.exhausted() is None
    b.charge(tokens=1_000, cny=0.0)
    assert "token" in b.exhausted()


def test_exhausted_on_cost():
    b = RunBudget(total_tokens=100_000, total_cny=0.5, total_seconds=600)
    b.charge(tokens=10, cny=0.5)
    assert "¥" in b.exhausted()


def test_exhausted_on_wall_clock():
    clock = iter([0.0, 0.0, 700.0])
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600,
                  clock=lambda: next(clock))
    b.start()
    assert b.exhausted() is None
    assert "时间" in b.exhausted()


def test_wall_clock_not_checked_before_start():
    """没 start 就不该有时间判定 —— 否则单测直接构造就会误触发。"""
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=0.0)
    assert b.exhausted() is None


def test_exhaustion_reports_the_kind():
    """种类要能与消息分开取：token / 金额归模型，墙钟归评测调度器。"""
    b = RunBudget(total_tokens=1_000, total_cny=2.0, total_seconds=600)
    assert b.exhaustion() is None
    b.charge(tokens=1_000, cny=0.0)
    assert b.exhaustion()[0] == "tokens"

    b2 = RunBudget(total_tokens=100_000, total_cny=0.5, total_seconds=600)
    b2.charge(tokens=10, cny=0.5)
    assert b2.exhaustion()[0] == "cny"

    clock = iter([0.0, 700.0])
    b3 = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600,
                   clock=lambda: next(clock))
    b3.start()
    kind, msg = b3.exhaustion()
    assert kind == "wall"
    assert msg == "时间预算耗尽：700s / 600s"


def test_exhausted_still_returns_just_the_message():
    """cli.run_once 在用 exhausted() 的返回值，语义不能变。"""
    b = RunBudget(total_tokens=1_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=1_000, cny=0.0)
    assert b.exhausted() == b.exhaustion()[1]


def test_spent_accessors():
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=1_200, cny=0.05)
    b.charge(tokens=800, cny=0.03)
    assert b.spent_tokens == 2_000
    assert abs(b.spent_cny - 0.08) < 1e-9


def test_tiny_budget_is_not_rounded_to_zero_in_message():
    """--budget 0.001 时上限不该显示成 ¥0.00 —— 那读起来像 bug。"""
    b = RunBudget(total_tokens=100_000, total_cny=0.001, total_seconds=600)
    b.charge(tokens=10, cny=0.0612)
    msg = b.exhausted()
    assert "0.001" in msg, msg
    assert "/ ¥0.00 " not in msg and not msg.endswith("/ ¥0.00")


def test_cny_allocates_by_remaining_failures():
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    assert abs(b.cny_for_failure(remaining_failures=4) - 0.5) < 1e-9
    b.charge(tokens=0, cny=0.5)
    assert abs(b.cny_for_failure(remaining_failures=3) - 0.5) < 1e-9


def test_cny_saved_flows_to_later_failures():
    """与 token 同形状：前面省下的自动流给后面难的。"""
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=0, cny=0.1)
    assert abs(b.cny_for_failure(remaining_failures=1) - 1.9) < 1e-9


def test_cny_has_no_floor():
    """token 有下限（再紧也要给一次有意义尝试），金额没有 —— 给下限
    就等于让闸失效，而那正是要挡住的事。"""
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    b.charge(tokens=0, cny=2.0)
    assert b.cny_for_failure(remaining_failures=1) == 0.0


def test_cny_zero_remaining_failures_returns_all():
    b = RunBudget(total_tokens=100_000, total_cny=2.0, total_seconds=600)
    assert abs(b.cny_for_failure(remaining_failures=0) - 2.0) < 1e-9


def test_remaining_cny_never_negative():
    b = RunBudget(total_tokens=100_000, total_cny=1.0, total_seconds=600)
    b.charge(tokens=0, cny=1.5)
    assert b.remaining_cny() == 0.0
