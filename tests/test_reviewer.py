"""裁判模型：唯一一层由 LLM 产出的信号。

这里钉的核心只有一条 —— **它没有否决权**。判 suspicious 时判定不变、补丁照常
进交付分支、报告多一行。一个能被说服的判定者等于没有判定者，而这一层的输入
（diff、traceback）恰恰是最容易把一个模型说服的东西。
"""
import json
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk
from harness.usage import Usage

from aifix.adapters.base import Failure
from aifix.agents.reviewer import build_prompt, parse_review
from aifix.cli import run_once
from aifix.config import AifixConfig

_SRC = '''def add(a, b):
    return a - b        # bug: 应为 a + b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5
'''

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})

_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b        # bug: 应为 a + b\n"
    "+    return a + b\n"
)


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    from harness.llm.base import ToolCallDelta
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "rv"
    (r / "tests").mkdir(parents=True)
    (r / "calc.py").write_text(_SRC, encoding="utf-8")
    (r / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _facts(repo: Path, run_id: str) -> list[dict]:
    p = repo / ".aifix" / "runs" / run_id / "facts.jsonl"
    return [json.loads(ln) for ln in
            p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# —— 解析 ——


def test_parses_a_well_formed_review():
    r = parse_review(json.dumps({"verdict": "suspicious", "reason": "硬编码"}))
    assert r is not None and r.is_suspicious and r.reason == "硬编码"


def test_plausible_is_not_suspicious():
    r = parse_review(json.dumps({"verdict": "plausible", "reason": "看着对"}))
    assert r is not None and not r.is_suspicious


@pytest.mark.parametrize("raw", [
    "不是 JSON",
    json.dumps({"verdict": "maybe", "reason": "说不好"}),   # 枚举外的取值
    json.dumps({"reason": "缺 verdict"}),
])
def test_unparseable_review_is_none_not_a_default_verdict(raw):
    """解析失败必须返回 None —— 退回任何一个默认判断都是在无中生有。

    退 suspicious 会让一个 JSON 输出不合规的模型把每个补丁都标红；
    退 plausible 是拿一次失败的调用给补丁背书。
    """
    assert parse_review(raw) is None


# —— 提示词 ——


def _failure() -> Failure:
    return Failure(test_id="tests/test_calc.py::test_add", classname="c",
                   name="test_add", message="assert -1 == 5",
                   trace="Traceback …")


def test_prompt_marks_the_diagnosis_as_an_inference():
    """诊断必须标明是推断。

    不标的话裁判会拿「补丁没改诊断点名的文件」直接判可疑，而诊断本身错了是
    常态（纯断言失败时 suspect_file 就是按包名猜的）—— 这一层量到的就成了
    「补丁跟 Detector 合不合得来」。
    """
    p = build_prompt(_failure(), _PATCH, {"root_cause": "减号", "fix_strategy": "改"})
    assert "可能是错的" in p


def test_prompt_survives_a_missing_diagnosis():
    p = build_prompt(_failure(), _PATCH, None)
    assert "没有可用的诊断" in p
    assert _PATCH.strip() in p


def test_long_diff_is_clipped():
    p = build_prompt(_failure(), "+x\n" * 20_000, None)
    assert "已截断" in p
    assert len(p) < 40_000


# —— 接线：它没有否决权 ——


async def _run(repo: Path, run_id: str, reviewer_turns, **cfg):
    return await run_once(
        repo, AifixConfig(reviewer_check=True, **cfg), run_id=run_id,
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                                _text("已修复")]),
        reviewer_client=_Scripted(reviewer_turns))


async def test_suspicious_verdict_changes_nothing(repo):
    """裁判喊可疑 —— 判定、交付、退出路径全都不动，只是报告多一行。

    这是这一层的全部边界。它一旦能改 verdict，`verify.py` 第一行那句
    「唯一有资格说修好了的地方，且不含任何 LLM」就不成立了。
    """
    state = await _run(repo, "rv1", [_text(json.dumps(
        {"verdict": "suspicious", "reason": "只改了符号没查调用方"}))])

    assert state["results"][0]["verdict"] == "better"          # 判定不变
    assert "a + b" in _git(repo, "show", "aifix/rv1:calc.py")  # 照常交付
    assert "裁判模型认为这个补丁可疑" in state["report_md"]
    assert "只改了符号没查调用方" in state["report_md"]
    assert "一个模型的看法" in state["report_md"]              # 出处要写明

    facts = {f["key"] for f in _facts(repo, "rv1")}
    assert "reviewer_suspicious" in facts


async def test_plausible_verdict_says_nothing_at_all(repo):
    """判 plausible 时一个字都不写。

    「裁判认为没问题」会被读成背书，而这一层没有资格背书任何东西 ——
    它的假阴性率没有人量过。
    """
    state = await _run(repo, "rv2", [_text(json.dumps(
        {"verdict": "plausible", "reason": "改动就在根本原因上"}))])

    assert state["results"][0]["verdict"] == "better"
    assert "值得多看一眼" not in state["report_md"]
    facts = {f["key"] for f in _facts(repo, "rv2")}
    assert "reviewer_suspicious" not in facts


async def test_a_broken_reviewer_does_not_block_delivery(repo):
    """裁判吐垃圾 / 端点炸了，补丁照常交付，这一层整个不发声。"""
    state = await _run(repo, "rv3", [_text("我觉得吧，这个补丁嘛……")])

    assert state["results"][0]["verdict"] == "better"
    assert "a + b" in _git(repo, "show", "aifix/rv3:calc.py")
    facts = {f["key"] for f in _facts(repo, "rv3")}
    assert "reviewer_suspicious" not in facts


async def test_reviewer_tokens_are_billed_to_the_run(repo):
    """裁判花的钱要记进 run 的账。

    不记的话 `budget_usd` 那道闸就有一个口子：钱真花出去了，闸上看不见。
    """
    with_reviewer = await _run(repo, "rv4", [_text(json.dumps(
        {"verdict": "plausible", "reason": "ok"}))])
    without = await run_once(
        repo, AifixConfig(reviewer_check=False), run_id="rv5",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                                _text("已修复")]))

    assert with_reviewer["spent_tokens"] > without["spent_tokens"]


async def test_off_by_default(repo):
    """默认不开：它要花钱，而且是这几层里唯一不可复现的。"""
    assert AifixConfig().reviewer_check is False

    state = await run_once(
        repo, AifixConfig(), run_id="rv6",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                                _text("已修复")]))

    assert state["results"][0]["verdict"] == "better"
    facts = {f["key"] for f in _facts(repo, "rv6")}
    assert "reviewer_suspicious" not in facts


# —— 路由缺席时不许悄悄回退到 fixer ——


def test_reviewer_route_is_not_built_from_the_environment_by_default():
    """`reviewer` 默认是 None，不是一个凭空构造出来的 HarnessConfig。

    这不是风格问题。`default_factory=HarnessConfig` 会**无条件构造**一个
    HarnessConfig，而它自己的 env_prefix 是 `HARNESS_` —— 于是环境里任何一个
    格式不对的 `HARNESS_*` 变量都会让 `AifixConfig()` 当场抛 SettingsError，
    哪怕 detector / fixer 都配全了、哪怕裁判这一层根本没打开。
    """
    assert AifixConfig().reviewer is None


def test_reviewer_adds_no_new_startup_failure_path(monkeypatch):
    """第三条路由不该改变「配置写错时怎么炸」。

    加 reviewer 的第一版用了 `default_factory=HarnessConfig`，于是没设
    `AIFIX_REVIEWER__*` 时也会去构造一个 HarnessConfig，而它读的是 `HARNESS_*`
    —— 一个格式不对的外部变量原本抛 pydantic.ValidationError（挂在 detector /
    fixer 上，`hide_input_in_errors` 管得住），变成了从 default_factory 里穿出来
    的 SettingsError。`tests/test_config.py` 那条防密钥泄漏的哨兵测试当场变红，
    而它红的理由不是泄漏，是**它守的那条路已经不是原来那条了**。

    注意这里**没有**声称写错的 `HARNESS_*` 不会让启动失败 —— 它一直会，那正是
    那条哨兵测试的前提。这条钉的只是：异常的种类和归属没被第三条路由改掉。
    """
    import pydantic

    for route in ("DETECTOR", "FIXER"):
        monkeypatch.setenv(f"AIFIX_{route}__MODEL", "m")
    monkeypatch.setenv("HARNESS_MODEL_PRICE_TIERS_BY_MODEL", "{{ 不是合法 JSON")

    with pytest.raises(pydantic.ValidationError) as e:
        AifixConfig()
    # 错误挂在真正配了的那两条路由上，没有多出一条 reviewer
    assert "reviewer" not in str(e.value)


def test_startup_refuses_when_the_layer_is_on_but_unrouted():
    from aifix.cli import require_reviewer_route

    with pytest.raises(SystemExit) as e:
        require_reviewer_route(AifixConfig(reviewer_check=True))
    msg = str(e.value)
    assert "拒绝启动" in msg
    assert "不会自动退回 fixer" in msg


def test_startup_is_fine_when_off_or_routed():
    from harness.config import HarnessConfig

    from aifix.cli import require_reviewer_route

    require_reviewer_route(AifixConfig())                      # 关着
    require_reviewer_route(AifixConfig(                        # 开着且配了路由
        reviewer_check=True, reviewer=HarnessConfig(model="m")))


async def test_unrouted_reviewer_stays_silent_instead_of_using_the_fixer(repo):
    """绕过 CLI 直接调 run_once（评测、别人的脚本）时的第二道闸。

    退回 fixer 那条路由是这里唯一「顺手」的选择，也恰恰是最坏的：同一个模型
    自己验自己，这一层看起来在工作、实际什么都没验。
    """
    state = await run_once(
        repo, AifixConfig(reviewer_check=True), run_id="rv7",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([_tool("apply_patch", json.dumps({"diff": _PATCH})),
                                _text("已修复")]))

    assert state["results"][0]["verdict"] == "better"
    facts = {f["key"] for f in _facts(repo, "rv7")}
    assert "reviewer_suspicious" not in facts
    assert "reviewer_failed" not in facts      # 不是「试了没成」，是压根没试
