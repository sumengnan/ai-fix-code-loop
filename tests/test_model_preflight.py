"""模型可达性预检：连不上就当场停，别拖到熔断。

不预检的话，表现是——每一轮都修不好 → 重试三次 → 连续失败熔断 → 报告写
「连续 N 个 failure 均未修复，疑似系统性问题」。跑了几十分钟，最后给出一句
指错方向的诊断，而真相是 API key 配错了或端点不通。

这与 preflight 里那段已经写下的话是同一个形状：「这个项目里最贵的失败一向
不是崩溃，是指错方向的诊断。」
"""
from pathlib import Path

from harness.llm.base import StreamChunk
from harness.usage import Usage

from aifix.cli import run_once
from aifix.config import AifixConfig
from aifix.graph import MODEL_ABORT_KIND
from aifix.nodes.preflight import probe_model


class _Dead:
    """连不上的端点。"""

    async def stream(self, messages, tools):
        raise ConnectionError("Connection refused")
        yield  # pragma: no cover —— 让它是个 async generator


class _Alive:
    async def stream(self, messages, tools):
        yield StreamChunk(type="text", text="pong")
        yield StreamChunk(type="done", usage=Usage(1, 1, 2))


# ---------------------------------------------------------------- 探针

async def test_probe_returns_none_when_reachable():
    assert await probe_model(AifixConfig(), client=_Alive()) is None


async def test_probe_points_at_credentials_and_network():
    """中止消息必须指向配置，而且**不能**看起来像模型没修好。"""
    why = await probe_model(AifixConfig(), client=_Dead())
    assert why
    assert "AIFIX_FIXER__BASE_URL" in why or "凭据" in why
    assert "修" not in why.split("\n")[0], "第一行不该让人以为是修复失败"


# ---------------------------------------------------------------- 接进 run

def _stub_probe(monkeypatch, reason=None):
    """替掉 cli 里那个探针，并记录它被调了几次。"""
    calls = []

    async def _fake(config, client=None):
        calls.append(config)
        return reason

    monkeypatch.setattr("aifix.cli.probe_model", _fake)
    return calls


async def test_run_aborts_before_baseline_when_the_model_is_unreachable(
        buggy_repo, monkeypatch):
    """必须挡在 baseline **之前**：全量测试要跑好几分钟，而这个错一秒就能查出来。"""
    _stub_probe(monkeypatch, reason="模型端点不可达：ConnectionError")
    st = await run_once(buggy_repo, AifixConfig(), run_id="dead")
    assert st["abort_kind"] == MODEL_ABORT_KIND
    # baseline 没跑：跑过的话这里会是 buggy_repo 里那一个真实失败
    assert not st["baseline_ids"]
    assert st["report_md"]


async def test_dry_run_never_touches_the_model(buggy_repo, monkeypatch):
    """`--dry-run` 的承诺是「不花一分钱，不调用任何模型」。

    预检也是模型调用 —— 不豁免的话，那句承诺就成了假话，而「接一个陌生项目
    先空跑一次看看有多少活」正是这个开关唯一的用途。
    """
    calls = _stub_probe(monkeypatch)
    st = await run_once(buggy_repo, AifixConfig(), run_id="dry", dry_run=True)
    assert not calls
    # 反向对照：baseline 确实跑了，说明它是**走完了**而不是提前退出
    assert st["baseline_ids"]


async def test_an_injected_client_is_never_probed(buggy_repo, monkeypatch):
    """注入了 client 就不探 —— 这条是回归测试，不是洁癖。

    探针一旦去消费注入进来的替身，就会吃掉它脚本里的**第一轮**，后面每一步
    都错位一格：诊断拿到本该给 fix 的补丁、fix 拿到下一轮的文本。实测过一次，
    9 个既有用例同时红，而症状（判定不对、成本对不上）没有一个字指向探针。
    """
    calls = _stub_probe(monkeypatch)
    await run_once(buggy_repo, AifixConfig(), run_id="stub",
                   fixer_client=_Alive(), detector_client=_Alive())
    assert not calls


# ---------------------------------------------------------------- 分类口径

async def test_eval_counts_it_as_a_harness_failure(history_repo, monkeypatch):
    """端点不通是**跑评测这台机器**的属性，不是被测模型的属性。

    记进修复成功率的分母，等于让模型替我们的网络背锅 —— 与收集错误、崩溃、
    墙钟耗尽同类。
    """
    import tempfile

    from aifix.eval.runner import run_task
    from aifix.eval.task import Task

    _stub_probe(monkeypatch, reason="模型端点不可达：ConnectionError")
    task = Task(task_id="t1", repo=str(history_repo["path"]),
                commit=history_repo["commit"], base_commit=history_repo["base"],
                test_files=history_repo["test_files"],
                target_test=history_repo["target"],
                gold_files=history_repo["gold_files"], origin="mined")
    with tempfile.TemporaryDirectory() as wd:
        res = await run_task(task, AifixConfig(), "stub", Path(wd))
    assert res.error and "评测故障" in res.error
    # 必须落在**这一条**分支上：baseline 还没跑，baseline_ids 是空的，
    # 「baseline 未复现目标用例」那条更笼统的分支会把它吸走并报「任务失效」
    assert "端点不可达" in res.error


async def test_a_missing_api_key_is_caught_by_the_probe(monkeypatch):
    """没配 key 是**最常见的第一次失败**，而客户端在**构造阶段**就会抛。

    构造若留在 try 之外，异常会裸穿出 run_once（探针挡在那个 try 之前），
    用户拿到一段 openai 的调用栈，没有报告、没有下一步 —— 而这恰恰是 preflight
    存在的全部理由。
    """
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError(
                "Missing credentials. Please pass an `api_key` ...")

    monkeypatch.setattr(
        "harness.llm.openai_compat.OpenAICompatibleClient", _Boom)
    why = await probe_model(AifixConfig())
    assert why and "AIFIX_FIXER__API_KEY" in why
