"""跑目标项目的测试时，不能把 aifix 自己的配置环境带进去。

实测（2026-07-30，issue #2 的真跑）：workflow 给 `aifix issue handle` 设了
`AIFIX_PRICE_MAP` / `AIFIX_BUDGET_CNY` / `AIFIX_FIXER__MODEL` 等，这些变量随
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
    monkeypatch.setenv("AIFIX_BUDGET_CNY", "15.0")
    monkeypatch.setenv("AIFIX_FIXER__MODEL", "some-model")
    monkeypatch.setenv("PATH_LIKE_NORMAL", "keep-me")


def test_aifix_vars_are_stripped(polluted):
    cmd = sanitized_command(["python", "-m", "pytest"])
    assert cmd[:1] == ["env"]
    assert "-u" in cmd and "AIFIX_BUDGET_CNY" in cmd
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
    assert "AIFIX_BUDGET_CNY" in raw


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

    monkeypatch.setenv("AIFIX_BUDGET_CNY", "15.0")
    monkeypatch.setenv("AIFIX_PRICE_MAP", '{"m":[1,1]}')

    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests" / "test_env.py").write_text(
        "import os\n\n\n"
        "def test_no_aifix_vars_leak_in():\n"
        "    leaked = sorted(k for k in os.environ if k.startswith('AIFIX_'))\n"
        "    assert leaked == [], leaked\n", encoding="utf-8")

    fs = await run_full_suite(tmp_path, [PytestAdapter()])
    assert fs.ids == set(), f"aifix 把自己的环境泄漏进了目标测试：{fs.failures}"
    assert fs.ran, "反向对照：测试确实跑了，不是一个都没收集到"


# ---------------------------------------------------------------- 测试超时

async def test_a_timeout_says_it_was_a_timeout_and_names_the_knob(tmp_path):
    """超时必须**明说是超时**，报出秒数和可调的旋钮。

    实测（2026-07-30，轮 9）：拿 aifix 自己当目标跑 `--dry-run`，套件在 worktree
    里跑了整整 900 秒被杀，而消息是「测试进程没能正常跑完（超时被杀 / 崩溃 /
    沙箱执行失败）」—— 三种原因揉成一句，不报数字、不指旋钮，而当时**唯一**
    的成因是那个写死的 900。

    `ExecResult.timed_out` 这个字段一直都在，只是没人读。
    """
    from aifix.adapters.pytest_adapter import PytestAdapter
    from aifix.nodes.baseline import run_full_suite

    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
        encoding="utf-8")

    with pytest.raises(RuntimeError) as e:
        # require_report=True 是核心循环每个调用点都传的（见 _check_report）
        await run_full_suite(tmp_path, [PytestAdapter()], timeout=1.0,
                             require_report=True)
    msg = str(e.value)
    assert "超时" in msg
    assert "1" in msg, "没报出超时的秒数"
    assert "AIFIX_TEST_TIMEOUT_SECONDS" in msg, "没指出可调的旋钮"
    # 反向对照：不能再把三种原因揉成一句
    assert "崩溃 / 沙箱执行失败" not in msg


def test_the_timeout_is_configurable():
    """写死 900 秒等于「套件超过 15 分钟的项目一律不支持」，而这件事
    没有任何地方写着，也没有任何办法改。"""
    from aifix.config import AifixConfig
    cfg = AifixConfig(test_timeout_seconds=3600.0)
    assert cfg.test_timeout_seconds == 3600.0
    assert AifixConfig().test_timeout_seconds >= 900.0


def test_every_test_run_takes_its_timeout_from_config():
    """三条跑测试的路径都要读配置里的超时，不能各自留着写死的默认值。

    漏掉一处的表现是：那条路径在长套件上照样被 900/300 秒掐死，而用户明明
    调大了旋钮 —— 一个「设了但不生效」的配置，比没有这个配置更糟。
    """
    import inspect

    from aifix.issue import handle
    from aifix.nodes import baseline, verify

    assert "test_timeout_seconds" in inspect.getsource(baseline.baseline_node)
    vsrc = inspect.getsource(verify)
    assert "test_timeout_seconds" in vsrc and "scoped_test_timeout_seconds" in vsrc
    assert "scoped_test_timeout_seconds" in inspect.getsource(handle)


async def test_a_failed_test_command_shows_what_it_actually_said(tmp_path):
    """测试命令跑挂时，**它自己的输出是最有用的那条线索** —— 不能扔掉。

    实测（2026-07-31）：Maven 侧的验收在 baseline 挂了，消息是「测试进程没能
    正常跑完（崩溃 / 沙箱执行失败）」，而 mvn 到底说了什么一个字都没有 ——
    要重跑一次加日志才查得下去。诊断信息在手里却不给，是这个项目一贯反对的
    那种「不报错，只是查不出来」。
    """
    from aifix.adapters.pytest_adapter import PytestAdapter
    from aifix.nodes.baseline import run_full_suite

    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # conftest 抛异常：pytest 会以非 0 退出并把原因写进 stderr
    (tmp_path / "conftest.py").write_text(
        "raise RuntimeError('这句话必须出现在 aifix 的报错里')\n",
        encoding="utf-8")

    with pytest.raises(RuntimeError) as e:
        await run_full_suite(tmp_path, [PytestAdapter()], require_report=True)
    assert "这句话必须出现在 aifix 的报错里" in str(e.value)
