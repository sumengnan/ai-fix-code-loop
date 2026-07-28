from aifix.budget import fmt_usd
from aifix.eval.score import render_table, summarize
from aifix.eval.task import TaskResult


def _r(**over):
    base = dict(task_id="t", model="M", locate_hit=True, suspect_file="a.py",
                verdict="better", attempts=1, tokens=1000, cost_usd=0.10,
                violations=0)
    base.update(over)
    return TaskResult(**base)


def test_rates_are_over_valid_tasks_only():
    """出错的任务不计入分母 —— 否则评测自己的故障会拉低被测系统的成绩。"""
    s = summarize([_r(), _r(verdict="same", locate_hit=False),
                   _r(error="克隆失败")])
    assert s.tasks == 2
    assert s.errors == 1
    assert s.fix_rate == 0.5
    assert s.locate_rate == 0.5


def test_all_errors_gives_zero_rates_not_crash():
    s = summarize([_r(error="炸了")])
    assert s.tasks == 0
    assert s.fix_rate == 0.0
    assert s.locate_rate == 0.0


def test_empty_input():
    s = summarize([])
    assert s.tasks == 0
    assert s.model == ""


def test_averages_and_totals():
    s = summarize([_r(cost_usd=0.10, attempts=1, violations=2),
                   _r(cost_usd=0.30, attempts=3, violations=1)])
    assert abs(s.avg_cost_usd - 0.20) < 1e-9
    assert abs(s.avg_attempts - 2.0) < 1e-9
    assert s.violations == 3, "越界是总数不是均值 —— 关心的是有没有、有几次"


def test_model_taken_from_results():
    assert summarize([_r(model="deepseek-v4-pro")]).model == "deepseek-v4-pro"


def test_table_has_one_row_per_model():
    a = summarize([_r(model="A")])
    b = summarize([_r(model="B", verdict="same")])
    md = render_table([a, b])
    assert "| A |" in md
    assert "| B |" in md
    assert "定位准确率" in md
    assert "越界尝试" in md


def test_zero_cost_with_tokens_spent_renders_as_unknown():
    """没配价格表时成本恒为 0，显示假的 $0.000 比不显示更糟。

    验收命令只设 API_KEY / BASE_URL / MODEL，不设 AIFIX_PRICE_MAP ——
    第一张跨模型对比表的成本列两行都会是 0，读起来像「极其便宜」，
    而不是「没数据」。report.py 早就这么处理了，score.py 不能再犯。
    """
    md = render_table([summarize([_r(cost_usd=0.0, tokens=12_345)])])
    assert "$0.000" not in md
    assert "未知" in md and "AIFIX_PRICE_MAP" in md


def test_zero_cost_without_tokens_is_a_real_zero():
    """一个 token 都没花（全 dry-run / 全出错）时，0 元就是 0 元。"""
    md = render_table([summarize([_r(cost_usd=0.0, tokens=0)])])
    assert "未知" not in md


def test_cost_uses_shared_money_formatter():
    """金额格式化复用 budget.fmt_usd，不另写一套。"""
    md = render_table([summarize([_r(cost_usd=0.1256)])])
    assert fmt_usd(0.1256) in md, md


def test_table_marks_error_count():
    """评测故障要单列出来 —— 不进分母，但也不能藏起来。"""
    s = summarize([_r(), _r(error="炸了")])
    row = render_table([s]).strip().splitlines()[-1]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "1", f"有效任务数应为 1：{row}"
    assert cells[-1] == "1", f"评测故障数应为 1：{row}"
