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


def test_cost_column_keeps_four_decimals():
    """成本列固定 4 位小数，不能复用 budget.fmt_usd。

    fmt_usd 是给预算总额设计的，对成本列典型落在的 1~10 分区间精度
    不够 —— 会把 $0.0201 和 $0.0249 都压成 $0.02。跨模型成本对比表
    最需要看的正是这一位，所以这里必须固定 4 位小数并且能分辨出来。
    """
    md = render_table([summarize([_r(model="A", cost_usd=0.0201)]),
                       summarize([_r(model="B", cost_usd=0.0249)])])
    assert "$0.0201" in md, md
    assert "$0.0249" in md, md


def test_cost_column_width_matches_across_rows():
    """两个量级的成本要能对齐着看，不能出现科学计数法。"""
    md = render_table([summarize([_r(cost_usd=0.1)]),
                       summarize([_r(cost_usd=0.000012)])])
    assert "e-" not in md, md
    assert "$0.1000" in md, md
    assert "$0.0000" in md, md


def test_table_shows_fraction_and_interval():
    """1/1 的 100% 必须在表格里就能看出「只有一个样本」。"""
    r = TaskResult(task_id="t", model="m", locate_hit=True, suspect_file="a.py",
                   verdict="better", attempts=1, tokens=10, cost_usd=0.1,
                   violations=0)
    table = render_table([summarize([r])])
    assert "100% (1/1" in table
    assert "21%" in table          # Wilson 下界，见 stats.wilson(1,1)
    # 反向钉死：不能只显示一个光秃秃的 100%
    assert "| 100% |" not in table


def test_zero_valid_tasks_renders_dash_not_zero_percent():
    """全是评测故障时，比率没有意义，不能显示 0%（会被读成「一个都没修好」）。"""
    r = TaskResult(task_id="t", model="m", locate_hit=False, suspect_file=None,
                   verdict="same", attempts=0, tokens=0, cost_usd=0.0,
                   violations=0, error="克隆失败")
    table = render_table([summarize([r])])
    assert "0%" not in table
    assert "—" in table


def test_table_marks_error_count():
    """评测故障要单列出来 —— 不进分母，但也不能藏起来。"""
    s = summarize([_r(), _r(error="炸了")])
    row = render_table([s]).strip().splitlines()[-1]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "1", f"有效任务数应为 1：{row}"
    assert cells[-1] == "1", f"评测故障数应为 1：{row}"


def test_table_renders_avg_tokens_column():
    """avg_tokens 算出来了却不渲染，等于成本列显示「未知」时表上没有任何
    消耗量信息 —— report.py 的同款处理带了 `（{tokens:,} tokens）`，
    对比表不能比单任务报告少这个信息。

    用没配价格表（cost_usd 恒为 0、tokens>0）的场景验证：这正是最需要
    tokens 列兜底的情形。
    """
    s = summarize([_r(cost_usd=0.0, tokens=12_345)])
    md = render_table([s])
    assert "平均 tokens" in md, md
    row = md.strip().splitlines()[-1]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert "12,345" in cells, f"表头与数据列错位了：{row}"
