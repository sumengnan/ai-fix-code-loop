"""跑目标项目的测试时，不能把 aifix 自己的配置环境带进去。

实测（2026-07-30，issue #2 的真跑）：workflow 给 `aifix issue handle` 设了
`AIFIX_PRICE_MAP` / `AIFIX_BUDGET_USD` / `AIFIX_FIXER__MODEL` 等，这些变量随
进程环境一路传进了**目标项目的 pytest 子进程**。而目标项目恰好是 aifix 自己，
它的 `AifixConfig` 读的正是这些变量 —— baseline 里凭空多出 15 个红。

后果不止是噪音：那些红会**进队列当成待修的 bug**，模型可能被派去修 aifix 自己
造成的污染；而「这个补丁没弄坏别的」这个判定，是在一个被自己弄脏的对照组上做的。
"""
import subprocess
import sys

import pytest

from aifix.testenv import aifix_vars_in_env, sanitized_command


@pytest.fixture
def polluted(monkeypatch):
    monkeypatch.setenv("AIFIX_BUDGET_USD", "2.0")
    monkeypatch.setenv("AIFIX_FIXER__MODEL", "some-model")
    monkeypatch.setenv("PATH_LIKE_NORMAL", "keep-me")


def test_aifix_vars_are_stripped(polluted):
    cmd = sanitized_command(["python", "-m", "pytest"])
    assert cmd[:1] == ["env"]
    assert "-u" in cmd and "AIFIX_BUDGET_USD" in cmd
    assert "AIFIX_FIXER__MODEL" in cmd
    # 原命令原样跟在后面
    assert cmd[-3:] == ["python", "-m", "pytest"]


def test_nothing_is_wrapped_when_there_is_nothing_to_strip(monkeypatch):
    """没有 AIFIX_ 变量时不加 `env` 前缀。

    无谓地包一层的代价是真实的：报错信息、进程树、`ps` 里看到的东西都多一层，
    而排查测试跑不起来时那一层会挡在最前面。
    """
    for k in list(aifix_vars_in_env()):
        monkeypatch.delenv(k, raising=False)
    assert sanitized_command(["pytest"]) == ["pytest"]


def test_unrelated_variables_survive(polluted):
    """只剥 AIFIX_ 前缀。目标项目的测试往往依赖 PATH / HOME / VIRTUAL_ENV，
    `env -i` 那种清空式做法会把测试直接跑不起来。"""
    cmd = sanitized_command(["pytest"])
    assert "PATH_LIKE_NORMAL" not in cmd


def test_the_child_process_really_does_not_see_them(polluted):
    """**真跑一次**，不只断言参数拼得对。

    `env -u` 的语义、参数顺序、以及它到底作用在哪一层，只有起一个真进程才能证明。
    断言命令行长什么样只能证明我们理解得自洽。
    """
    probe = ("import os, json;"
             "print(json.dumps(sorted(k for k in os.environ if k.startswith('AIFIX_'))))")
    cmd = sanitized_command([sys.executable, "-c", probe])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out

    # 反向对照：不剥的话它们确实在 —— 否则上面那条恒真
    raw = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, check=True).stdout
    assert "AIFIX_BUDGET_USD" in raw


async def test_every_test_running_path_is_sanitized(tmp_path):
    """三条跑测试的路径都要经过它：baseline 的全量、局部，以及 agent 的
    run_tests 工具。

    漏掉任一条，那条路径上的 baseline 与 verify 就会在不同的环境下测量 ——
    而两边都不报错，只是判定的依据不是同一套环境。
    """
    import inspect

    from aifix.nodes import baseline
    from aifix.tools import tests as tests_tool

    for mod in (baseline, tests_tool):
        src = inspect.getsource(mod)
        assert "sanitized_command" in src, mod.__name__

    # 精确到调用点：三处 exec 跑的都是适配器给的测试命令
    b = inspect.getsource(baseline)
    assert b.count("sanitized_command(adapter.") >= 2, "baseline 里少包了一处"


async def test_run_full_suite_hands_the_target_a_clean_environment(
        tmp_path, monkeypatch):
    """端到端：造一个「看到 AIFIX_ 变量就失败」的目标仓库，让 baseline 去跑它。

    上面几条断言的是命令拼得对、子进程看不到；这一条断言的是**核心循环真的
    用了那条命令** —— 三处调用点漏掉任何一处，这里就会红。
    """
    from aifix.adapters.pytest_adapter import PytestAdapter
    from aifix.nodes.baseline import run_full_suite

    monkeypatch.setenv("AIFIX_BUDGET_USD", "2.0")
    monkeypatch.setenv("AIFIX_PRICE_MAP", '{"m":[1,1]}')

    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests" / "test_env.py").write_text(
        "import os\n\n\n"
        "def test_no_aifix_vars_leak_in():\n"
        "    leaked = sorted(k for k in os.environ if k.startswith('AIFIX_'))\n"
        "    assert leaked == [], leaked\n", encoding="utf-8")

    fs = await run_full_suite(tmp_path, PytestAdapter())
    assert fs.ids == set(), f"aifix 把自己的环境泄漏进了目标测试：{fs.failures}"
    assert fs.ran, "反向对照：测试确实跑了，不是一个都没收集到"
