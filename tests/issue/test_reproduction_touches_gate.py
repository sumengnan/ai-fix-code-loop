"""复现测试不接触本项目代码时，退回重写 —— 只退一次。

ai-learning-helper#95 的形状：模型写出一条对 `EmptyHint.tsx` 做字符串 grep 的
pytest 测试，红检放行、报告写「修复 1/1」，而把补丁整个撤销、只留一句「还没加」
的注释，它照样绿。

见 docs/superpowers/specs/2026-08-05-reproduction-must-touch-project-design.md §4
"""
from aifix.agents.reproducer import Reproduction
from aifix.config import AifixConfig
from aifix.issue.handle import handle
from aifix.reproduce import ReproduceOutcome

from .test_issue_handle import _Gh, _payload, _state

# 不接触本项目的复现测试 —— #95 那条的形状。
_GREP_CODE = ('from pathlib import Path\n\n'
              'def test_x():\n'
              '    assert "生成5道ai题" in Path("web/src/x.tsx").read_text()\n')

# 正经的复现测试：import 了本项目的模块。
_REAL_CODE = ('from app.hint import SUGGESTIONS\n\n'
              'def test_x():\n'
              '    assert any("AI" in s for s in SUGGESTIONS)\n')


def _repro(code):
    return Reproduction(
        can_reproduce=True, test_file="tests/test_issue_95.py",
        test_code=code, target_test_id="tests/test_issue_95.py::test_x")


async def _run(tmp_path, codes):
    """按 `codes` 依次作答，返回 (结果, 复现器被调了几次, 每轮收到的 body)。"""
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "app").mkdir(exist_ok=True)      # 让 app 成为可 import 的顶层名
    calls: list[str] = []

    async def _reproduce_fn(repo, adapters, config, title, issue_body):
        calls.append(issue_body)
        return ReproduceOutcome(_repro(codes[min(len(calls) - 1, len(codes) - 1)]))

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        return _state()

    gh = _Gh()
    res = await handle(_payload("/aifix 建议列表少了一条"), tmp_path,
                       AifixConfig(), gh,
                       reproduce_fn=_reproduce_fn, red_check_fn=_red,
                       run_fn=_run_fn, publish=lambda *a, **k: True,
                       git=lambda *a, **k: "")
    return res, len(calls), calls


async def test_a_touching_reproduction_is_not_retried(tmp_path):
    """正经的复现测试一次过 —— 这道闸对绝大多数 run 必须是零成本的。"""
    _res, n, _bodies = await _run(tmp_path, [_REAL_CODE])
    assert n == 1


async def test_a_grep_style_reproduction_is_sent_back_once(tmp_path):
    """#95 那条：退回重写，第二轮写对了就用第二轮的。"""
    _res, n, _bodies = await _run(tmp_path, [_GREP_CODE, _REAL_CODE])
    assert n == 2


async def test_the_rewrite_is_told_why_it_was_rejected(tmp_path):
    """重写那一轮必须带上理由。不带的话模型只会原样再写一遍，白花一轮的钱。"""
    _res, _n, bodies = await _run(tmp_path, [_GREP_CODE, _REAL_CODE])
    assert "import" in bodies[1]
    assert bodies[1] != bodies[0]


async def test_it_only_goes_back_once(tmp_path):
    """第二次仍不过就**放行** —— 误报的成本必须封顶。

    一条只用 subprocess 跑 CLI 的合法测试重写多少次都过不了这道闸，无限退回
    会把一次本来能成的 run 拖死在一个启发式判据上。规格 §4。
    """
    _res, n, _bodies = await _run(tmp_path, [_GREP_CODE, _GREP_CODE])
    assert n == 2


async def test_passing_through_still_reaches_the_fix_step(tmp_path):
    """放行不是放弃：第二次仍不过时，这一轮照常往下走到修复。"""
    res, _n, _bodies = await _run(tmp_path, [_GREP_CODE, _GREP_CODE])
    assert res.path != "no_repro"


async def test_answering_never_triggers_a_rewrite(tmp_path):
    """答复那一路不重跑复现器 —— 哪怕存下来的那条测试过不了这道闸。

    它取自上一轮的隐藏标记，那一步零模型调用。在这里退回重写会凭空多花一次，
    而且未必写出同一条测试：人回答的是针对**上一条**测试的问题，换一条就答非
    所问（tests/issue/test_issue_ask.py 里那条同名的守卫记着同一件事）。
    """
    from aifix.runtime import pending as pending_store

    from .test_issue_ask import _ASK, _REPRO

    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "app").mkdir(exist_ok=True)
    calls: list[int] = []

    async def _reproduce_fn(*a, **k):
        calls.append(1)
        return ReproduceOutcome(_repro(_GREP_CODE))

    async def _red(*a, **k):
        return True, ""

    async def _run_fn(repo, cfg, **kw):
        return _state()

    marker = pending_store.encode_marker({
        **_ASK, "repro": {"test_file": _REPRO.test_file,
                          "test_code": _GREP_CODE,      # 存的就是过不了闸的那种
                          "target_test_id": _REPRO.target_test_id}})
    gh = _Gh()
    gh.status_body = lambda issue: marker
    await handle(_payload("/aifix 1"), tmp_path, AifixConfig(), gh,
                 reproduce_fn=_reproduce_fn, red_check_fn=_red, run_fn=_run_fn,
                 publish=lambda *a, **k: True, git=lambda *a, **k: "")
    assert calls == []
