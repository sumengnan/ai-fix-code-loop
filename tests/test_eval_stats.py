from aifix.eval.stats import wilson


def test_single_perfect_sample_is_not_a_conclusion():
    """1/1 = 100% 的区间下界必须远离 100% —— 这是整个模块存在的理由。"""
    lo, hi = wilson(1, 1)
    assert 0.20 < lo < 0.22, lo        # 精确值 0.2065
    assert hi == 1.0


def test_larger_sample_narrows_the_interval():
    lo1, hi1 = wilson(6, 10)
    lo2, hi2 = wilson(60, 100)
    assert (hi1 - lo1) > (hi2 - lo2) * 2.5
    # 都以 0.6 为中心附近
    assert lo1 < 0.6 < hi1 and lo2 < 0.6 < hi2


def test_known_values():
    """对着教科书数值断言，不是断言「区间存在」。"""
    lo, hi = wilson(0, 10)
    assert lo == 0.0
    assert 0.27 < hi < 0.29            # 精确值 0.2775
    lo, hi = wilson(5, 10)
    assert 0.23 < lo < 0.25            # 0.2366
    assert 0.75 < hi < 0.77            # 0.7634


def test_zero_sample():
    assert wilson(0, 0) == (0.0, 0.0)
