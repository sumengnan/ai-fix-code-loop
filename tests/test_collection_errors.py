"""baseline 里全是收集错误时不许当成工单排队。

这条缺口是上一条修复（`AIFIX_TEST_PYTHON`）的实测挖出来的：拿 aifix 自己的
解释器去跑一个依赖不在这个 venv 里的项目，pytest 收集阶段整轮中断（exit 2），
**照样写出一份完整的 JUnit 报告**，里面躺着一条条文件级 `<error>`。
`_check_report` 拦的是「一份报告都没写出来」，所以它一个异常都不会抛：
11 条 `ModuleNotFoundError` 被 `make_test_id` 老老实实翻译成 11 个可重跑的
node id，进 baseline_ids，进 queue，然后 Detector 和 Fixer 被派去修
「目标机器上没装某个包」这件事 —— 真花钱，且报告写的是「模型没修好」。

本文件里凡是「真实收集错误」的用例一律**真跑一次 pytest**，不手写 XML：
这个项目吃过手写 XML 喂出来的绿灯（`locate_source` 的 bug 就是那么活下来的）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aifix.adapters.maven_adapter import MavenAdapter
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.config import AifixConfig
from aifix.graph import COLLECTION_ABORT_KIND, new_state
from aifix.nodes.baseline import (COLLECTION_ABORT_MIN_COUNT, baseline_node,
                                  collection_error_abort, file_level_ids)

_BAD_IMPORT = "import 一个绝不存在的模块_{n}\n\n\ndef test_x():\n    assert True\n"
_REAL_FAIL = "def test_fail_{n}():\n    assert 1 == 2\n"
_PASSING = "def test_ok_{n}():\n    assert True\n"


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """一个能被 pytest 收集的最小项目。**不建 git**：baseline_node 只需要
    worktree_path 指向一个目录，git 是 preflight / Worktree 的事，那两处的
    接线另有测试盯着（tests/test_nodes_preflight_baseline.py）。"""
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n",
                                     encoding="utf-8")
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    return repo


def _state(repo: Path, **cfg):
    """直接喂给 baseline_node 的 state。

    adapter_names 手工填死而不走 preflight：这几条测试要验的是 baseline 拿到
    报告之后的判定，探测与「工作区干净」是另一件事。
    """
    st = new_state(repo, AifixConfig(test_python=None, **cfg), run_id="r1")
    st["adapter_names"] = ["pytest"]
    st["worktree_path"] = str(repo)
    return st


def _maven_ids(files: int, cases: int) -> list[str]:
    """Maven 侧的 id：类级（裸类名）与用例级（`类#方法`）。

    用 Maven 而不是 pytest 造这几条比例用例，有两个理由。一是判据必须走
    适配器的 `is_file_level_id`，不能自己写一套 pytest 的 `::`；二是
    **pytest 根本产不出「真失败与收集错误混在一起」的报告**（见
    test_pytest_interrupts_collection_so_the_ratio_is_always_one），
    而 surefire 能：一个测试类 @BeforeAll 炸了，别的类照跑。
    """
    return ([f"demo.Boot{i}Test" for i in range(files)]
            + [f"demo.CalcTest#t{i}" for i in range(cases)])


# ---------------------------------------------------------------- 真实数据


async def test_a_real_collection_error_does_not_become_a_queue(tmp_path):
    """真跑一次 pytest 拿到的收集错误，一条都不许进队列。

    这是上一条修复实测出来的形状本身：退出码 0、报告正常渲染、
    「修复 0 / 11」看着像一个成绩。
    """
    repo = _repo(tmp_path, {
        f"tests/test_bad{i}.py": _BAD_IMPORT.format(n=i) for i in range(3)})
    out = await baseline_node(_state(repo))

    # 前提：这一跑确实产出了文件级 id —— 否则下面的断言什么都没验证
    assert out["baseline_ids"] == ["tests/test_bad0.py", "tests/test_bad1.py",
                                   "tests/test_bad2.py"], out["baseline_ids"]
    assert out["queue"] == []
    assert out["abort_kind"] == COLLECTION_ABORT_KIND
    assert out["abort"]


async def test_pytest_interrupts_collection_so_the_ratio_is_always_one(tmp_path):
    """一条收集错误就让 pytest 中断整轮收集 —— 真失败一个都不在报告里。

    这条实测钉住的是判据的适用边界：pytest 侧「文件级占比」在有收集错误时
    **恒为 1.0**，比例那一半只在 Maven 侧真正做功（surefire 不中断，别的
    测试类照跑）。这也说明「10 个真失败 + 1 个收集错误」这种 baseline 在
    pytest 上压根不可达。
    """
    repo = _repo(tmp_path, {"tests/test_bad0.py": _BAD_IMPORT.format(n=0),
                            "tests/test_real.py": _REAL_FAIL.format(n=0),
                            "tests/test_ok.py": _PASSING.format(n=0)})
    out = await baseline_node(_state(repo))
    assert out["baseline_ids"] == ["tests/test_bad0.py"], out["baseline_ids"]


async def test_a_repo_with_real_failures_is_still_queued(tmp_path):
    """反向断言：正常有失败的仓库必须照旧排队开修。

    没有这一条的话，一个「什么都拦」的实现也能让上面那条通过。
    """
    repo = _repo(tmp_path, {"tests/test_a.py": _REAL_FAIL.format(n=1),
                            "tests/test_b.py": _REAL_FAIL.format(n=2),
                            "tests/test_ok.py": _PASSING.format(n=0)})
    out = await baseline_node(_state(repo))
    assert out["baseline_ids"] == ["tests/test_a.py::test_fail_1",
                                   "tests/test_b.py::test_fail_2"]
    assert out["queue"] == out["baseline_ids"]
    assert out["abort"] is None
    assert out["abort_kind"] is None


async def test_one_lone_collection_error_is_still_queued(tmp_path):
    """计数下限的下侧：**单独一条**收集错误照旧排队。

    一个测试文件 import 不到东西，很可能就是这个仓库自己的 bug（模块被改名、
    忘了提交一个文件），那正是 aifix 该修的活。且代价有界 —— 队列里只有它
    一条，最多烧掉一个 failure 的预算。
    """
    repo = _repo(tmp_path, {"tests/test_bad0.py": _BAD_IMPORT.format(n=0)})
    out = await baseline_node(_state(repo))
    assert out["baseline_ids"] == ["tests/test_bad0.py"]
    assert out["queue"] == ["tests/test_bad0.py"]
    assert out["abort"] is None


async def test_the_knob_lets_collection_errors_through(tmp_path, capsys):
    """确认「这些收集错误就是仓库自己的 bug」时的逃生口，且它不静默。"""
    repo = _repo(tmp_path, {
        f"tests/test_bad{i}.py": _BAD_IMPORT.format(n=i) for i in range(3)})
    out = await baseline_node(_state(repo, allow_collection_errors=True))
    assert out["queue"] == out["baseline_ids"] != []
    assert out["abort"] is None
    assert "AIFIX_ALLOW_COLLECTION_ERRORS" in capsys.readouterr().err


# ---------------------------------------------------------------- 判据本身


def test_file_level_ids_go_through_the_adapter_not_pytest_syntax():
    """判据必须问适配器，不能自己写一套 `::`。

    `eval/mine` 曾经写死 `"::" not in i`，而 `::` 是 pytest 的语法 ——
    Maven 的 id 一个都没有，于是**每一个** Maven id 都被判成文件级。
    这里反过来：Maven 的用例级 id 带 `#`，写死 `::` 的实现会把它们全判成
    文件级，从而在一个纯用例失败的 Maven baseline 上误中止。
    """
    mixed = _maven_ids(files=2, cases=2)
    assert file_level_ids(mixed, [MavenAdapter()]) == ["demo.Boot0Test",
                                                     "demo.Boot1Test"]
    cases_only = _maven_ids(files=0, cases=4)
    assert file_level_ids(cases_only, [MavenAdapter()]) == []
    assert collection_error_abort(cases_only, [MavenAdapter()]) is None


@pytest.mark.parametrize("files,cases,aborts", [
    # 计数下限两侧（比例都是 1.0，只有条数在变）
    (COLLECTION_ABORT_MIN_COUNT - 1, 0, False),
    (COLLECTION_ABORT_MIN_COUNT, 0, True),
    # 比例两侧：恰好一半不拦，严格过半才拦
    (2, 2, False),
    (2, 1, True),
    # 用户举的那个必须放行的例子：10 个真失败 + 1 个收集错误
    (1, 10, False),
    # 少数派的收集错误：3 条，但只占 3/13
    (3, 10, False),
])
def test_threshold_boundaries(files, cases, aborts):
    """阈值的两侧各钉一条 —— 「全拦」和「全不拦」都要过不了这组。"""
    ids = _maven_ids(files, cases)
    got = collection_error_abort(ids, [MavenAdapter()])
    assert (got is not None) is aborts, (files, cases, got)


def test_an_empty_or_all_green_baseline_never_aborts():
    """全绿的 baseline 没有任何失败，占比无从谈起，绝不能除零或误判。"""
    assert collection_error_abort([], [PytestAdapter()]) is None


# ---------------------------------------------------------------- 消息


def test_the_message_says_it_is_not_the_model_and_gives_a_next_step():
    """中止消息要能让人看出这不是模型的问题，并给出可操作的下一步。"""
    adapter = PytestAdapter(python="/tmp/某个/解释器/python")
    msg = collection_error_abort(["tests/test_a.py", "tests/test_b.py"], [adapter])
    assert msg
    assert "不是模型" in msg
    assert "AIFIX_TEST_PYTHON" in msg
    assert "/tmp/某个/解释器/python" in msg, "没说清是哪个解释器收集不到"
    assert "tests/test_a.py" in msg, "没点名到底哪些文件收集失败"
    assert "AIFIX_ALLOW_COLLECTION_ERRORS" in msg, "没给逃生口"


def test_the_message_does_not_talk_about_python_on_a_maven_project():
    """Maven 工程上劝人换 Python 解释器是一句假话。

    本项目把「消息说了一件代码没做的事」与「数字造假」同等对待。
    """
    msg = collection_error_abort(_maven_ids(files=3, cases=0), [MavenAdapter()])
    assert msg
    assert "AIFIX_TEST_PYTHON" not in msg
    assert "pytest" not in msg


# ---------------------------------------------------------------- 报告


def _report_state(**over):
    st = {"run_id": "r1", "adapter_names": ["pytest"], "branch": "aifix/r1",
          "baseline_ids": [f"tests/test_bad{i}.py" for i in range(4)],
          "results": [], "spent_tokens": 0, "spent_usd": 0.0,
          "signals": [], "abort": None, "abort_kind": None}
    st.update(over)
    return st


def test_the_report_does_not_render_a_score_for_a_collection_abort():
    """「修复 0 / 4」在这种中止下长得和一个成绩一模一样，而这次根本没开修。

    分母是一批本就不该存在的工单数，分子是「一个都没轮到」。这一行正是
    用户在实测里看到的那句「修复 0 / 11」—— 它让一次故障读起来像一次失分。
    """
    from aifix.nodes.report import render_report

    md = render_report(_report_state(abort="baseline 不可信",
                                     abort_kind=COLLECTION_ABORT_KIND))
    assert "0 / 4" not in md, md
    assert "不可信" in md, "去掉了数字却没说清为什么没有这个数"


def test_the_report_still_renders_the_score_when_the_run_really_ran():
    """反向断言：正常收场、以及预算耗尽这类**正常**中止，照旧印修复数。

    没有这一条的话，一个「永远不印修复数」的实现也能让上面那条通过。
    """
    from aifix.nodes.report import render_report

    assert "0 / 4" in render_report(_report_state())
    assert "0 / 4" in render_report(_report_state(
        abort="token 预算耗尽：50000 / 50000", abort_kind="tokens"))


# ---------------------------------------------------------------- 评测口径


async def test_eval_counts_it_as_an_eval_fault_not_a_model_failure(
        history_repo, tmp_path, monkeypatch):
    """环境坏了不是模型的成绩 —— 这条中止必须走评测故障那一路。

    把「目标机器上没装某个包」记成模型的失分，正是这条修复要消灭的东西。
    与墙钟中止同类：它是**跑 aifix 的这台机器**的属性，不是被测模型的属性，
    换一台装好依赖的机器同一个模型就能修 —— 记进比率分母等于让模型替
    我们的环境背锅。
    """
    from aifix.eval.runner import run_task
    from tests.test_eval_runner import _fake_run_once, _task

    t = _task(history_repo)
    _fake_run_once(monkeypatch, t.target_test,
                   abort="baseline 全是收集错误",
                   abort_kind=COLLECTION_ABORT_KIND)
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w")
    assert r.error is not None, "被记成了模型的失败"
    assert "评测故障" in r.error
    assert r.verdict == "same"


async def test_eval_still_counts_token_budget_as_the_model_score(
        history_repo, tmp_path, monkeypatch):
    """反向断言：token 预算耗尽仍然是模型的真实成绩，不许被顺手改成故障。"""
    from aifix.eval.runner import run_task
    from tests.test_eval_runner import _fake_run_once, _task

    t = _task(history_repo)
    _fake_run_once(monkeypatch, t.target_test,
                   abort="token 预算耗尽：50000 / 50000", abort_kind="tokens")
    r = await run_task(t, AifixConfig(), "假模型", tmp_path / "w")
    assert r.error is None


def test_cli_run_exits_nonzero_on_a_collection_abort(monkeypatch, capsys):
    """流水线里「跑完了」和「环境坏了没跑成」不能是同一个退出码。

    预算耗尽退 0 是对的（那是正常收场），这一条不是：baseline 压根不可信。
    """
    import aifix.cli as cli

    async def fake_run_once(*a, **kw):
        return {"report_md": "# 报告", "abort": "x",
                "abort_kind": COLLECTION_ABORT_KIND}

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    # 用真的 parser 造 args，不手搓命名空间：手搓的那种每加一个 CLI 开关就
    # 断一次（--quiet 就断过），而且它验证不了那些字段真的存在于 parser 里。
    args = cli.build_parser().parse_args(["run", "."])
    with pytest.raises(SystemExit) as ei:
        cli._cmd_run(args)
    assert ei.value.code == 1
    assert "# 报告" in capsys.readouterr().out


def test_cli_run_exits_zero_on_a_budget_abort(monkeypatch, capsys):
    """反向断言：预算耗尽仍然退 0，不许顺手把所有中止都改成 1。"""
    import aifix.cli as cli

    async def fake_run_once(*a, **kw):
        return {"report_md": "# 报告", "abort": "token 预算耗尽",
                "abort_kind": "tokens"}

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    # 用真的 parser 造 args，不手搓命名空间：手搓的那种每加一个 CLI 开关就
    # 断一次（--quiet 就断过），而且它验证不了那些字段真的存在于 parser 里。
    args = cli.build_parser().parse_args(["run", "."])
    cli._cmd_run(args)
    assert "# 报告" in capsys.readouterr().out
