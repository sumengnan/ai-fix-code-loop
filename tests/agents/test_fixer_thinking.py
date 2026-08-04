"""fixer 的思考模式：默认关，验证不通过之后升级开。"""
from aifix.config import AifixConfig
from aifix.nodes.fix import fixer_route


def _extra(cfg, attempt):
    return fixer_route(cfg, attempt).llm_extra_body


def test_thinking_is_off_on_the_first_attempt():
    """第 1 轮关掉。修 bug 的活大多是机械的（读代码、改几行、跑测试），
    而实测有一轮的输出预算被推理全部吃掉、正文一个字没吐。"""
    assert _extra(AifixConfig(), attempt=1).get("enable_thinking") is False


def test_thinking_turns_on_once_a_full_attempt_failed_verification():
    """第 2 轮起开。

    attempt 只在 verify 判了 not-better **之后**才递增（见 verify_node），
    所以 attempt≥2 的含义精确地就是「上一轮写出来的代码没通过验证」——
    这正是该换用更贵的思考模式的时刻。

    守卫重试（空 diff / 巨型 diff）不递增 attempt，也就不会触发升级：那是
    「没写出代码」，不是「写的代码没通过验证」，两者要的补救完全不同。
    """
    assert _extra(AifixConfig(), attempt=2).get("enable_thinking") is True
    assert _extra(AifixConfig(), attempt=3).get("enable_thinking") is True


def test_the_escalation_point_is_configurable():
    cfg = AifixConfig(fixer_thinking_after_attempt=3)
    assert _extra(cfg, attempt=2).get("enable_thinking") is False
    assert _extra(cfg, attempt=3).get("enable_thinking") is True


def test_escalation_can_be_turned_off():
    """0 表示永不升级 —— 一路按 fixer_thinking 那个基准跑。"""
    cfg = AifixConfig(fixer_thinking_after_attempt=0)
    assert _extra(cfg, attempt=9).get("enable_thinking") is False


def test_thinking_can_be_on_from_the_start():
    cfg = AifixConfig(fixer_thinking=True)
    assert _extra(cfg, attempt=1).get("enable_thinking") is True


def test_none_means_do_not_send_the_parameter_at_all():
    """None = 随端点默认。**升级仍然生效** —— 「不表态」不等于「不许升级」，
    而升级恰恰是要在这一刻明确表态。"""
    cfg = AifixConfig(fixer_thinking=None)
    assert "enable_thinking" not in _extra(cfg, attempt=1)
    assert _extra(cfg, attempt=2).get("enable_thinking") is True


def test_the_route_keeps_the_endpoint_and_credentials():
    """只动思考模式，端点/凭据/模型名一个字都不许变。"""
    cfg = AifixConfig()
    r = fixer_route(cfg, attempt=2)
    assert r.model == cfg.fixer.model
    assert r.base_url == cfg.fixer.base_url
    assert r.api_key == cfg.fixer.api_key


def test_the_baseline_route_is_not_mutated():
    """model_copy 而不是就地改：cfg.fixer 是整个 run 共用的一份。"""
    cfg = AifixConfig()
    fixer_route(cfg, attempt=2)
    assert "enable_thinking" not in cfg.fixer.llm_extra_body


# —— 接线：光有 fixer_route 正确不够，它得真的被用上 ——


async def test_the_route_is_what_the_loop_actually_gets(buggy_repo, monkeypatch):
    """两轮真跑，记下每一轮建客户端时拿到的路由。

    没有这一条，`fixer_route` 算得再对也可能压根没接进 AgentLoop —— 而那种
    漏接是静默的：思考模式一直是端点默认，报告里看不出任何异常。
    """
    import aifix.nodes.fix as fix_mod
    from aifix.config import AifixConfig

    seen: list[dict] = []
    real = fix_mod.OpenAICompatibleClient

    def _spy(route):
        seen.append(dict(route.llm_extra_body))
        return real(route)

    monkeypatch.setattr(fix_mod, "OpenAICompatibleClient", _spy)

    from pathlib import Path

    from aifix.adapters.base import Failure
    from aifix.graph import new_state

    tid = "tests/test_calc.py::test_add"
    cfg = AifixConfig()
    for attempt in (1, 2):
        st = new_state(Path(buggy_repo), cfg, run_id="t")
        st["attempt"] = attempt
        st["worktree_path"] = str(buggy_repo)
        st["adapter_names"] = ["pytest"]
        st["baseline_ids"] = [tid]
        st["current"] = tid
        st["_failures"] = {tid: Failure(test_id=tid, classname="c",
                                        name="test_add", message="m",
                                        trace="t")}
        st["_owners"] = {tid: "pytest"}
        try:
            await fix_mod.fix_node(st)
        except Exception:       # noqa: BLE001 —— 只关心建客户端那一刻
            pass

    assert len(seen) == 2, seen
    assert seen[0].get("enable_thinking") is False    # 第 1 轮：便宜的那一档
    assert seen[1].get("enable_thinking") is True     # 第 2 轮：升级
