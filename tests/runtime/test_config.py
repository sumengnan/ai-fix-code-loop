import pytest

from aifix.config import AifixConfig


def test_defaults():
    c = AifixConfig()
    assert c.max_attempts == 3
    assert c.budget_cny == 15.0
    assert c.price_currency == "USD"
    assert c.usd_to_cny == 7.2
    assert c.budget_tokens == 500_000
    assert c.budget_wall_seconds == 1800.0
    assert c.fixer_max_steps == 25


def test_已移除的_allow_test_edits_不会复活(monkeypatch):
    """测试文件守卫无条件生效，没有任何配置项能关掉它。

    这个字段声明过、但从来没有被 src/ 里任何地方读过（守卫是无条件的），
    已删除。这条测试钉的不是「删干净了」而是「不许再悄悄加回来」：接一个
    bool 上去就能让 agent 改测试，而测试正是这个项目判「修好了」的 oracle。
    """
    monkeypatch.setenv("AIFIX_ALLOW_TEST_EDITS", "true")
    c = AifixConfig()
    assert not hasattr(c, "allow_test_edits")
    # 顺带钉住 extra="ignore" 的代价：环境变量被静默吸收，不报错也不生效。
    # 这是有意的（不能让上游多设一个变量就让所有人起不来），但它意味着
    # 拼错的配置名同样不会报错 —— 知道这一点，才不会误以为「设了就生效」。
    assert "allow_test_edits" not in c.model_dump()


def test_nested_env_overrides(monkeypatch):
    monkeypatch.setenv("AIFIX_DETECTOR__MODEL", "glm-4.6")
    monkeypatch.setenv("AIFIX_FIXER__MODEL", "deepseek-chat")
    c = AifixConfig()
    assert c.detector.model == "glm-4.6"
    assert c.fixer.model == "deepseek-chat"


def test_scalar_env_override(monkeypatch):
    monkeypatch.setenv("AIFIX_MAX_ATTEMPTS", "5")
    assert AifixConfig().max_attempts == 5


def test_price_map_default_empty():
    assert AifixConfig().price_map == {}


def test_price_map_from_env(monkeypatch):
    """没有 price_map 就算不出成本，报告会显示假的 $0.00。"""
    monkeypatch.setenv("AIFIX_PRICE_MAP", '{"deepseek-v4-pro": [3, 6]}')
    assert AifixConfig().price_map["deepseek-v4-pro"] == [3.0, 6.0]


def test_price_map_rejects_tiered_format(monkeypatch):
    """分档表 [[上限,输入,输出]] 不是扁平价表，必须在加载时就拒绝。

    真实运行中传错格式，导致跑到一半才在成本计算里解包失败崩溃 ——
    token 已经花掉了。成本计算是装饰性的，不该有崩掉整个 run 的权力。
    """
    import pydantic
    monkeypatch.setenv("AIFIX_PRICE_MAP",
                       '{"deepseek-v4-pro": [[1000000, 3, 6]]}')
    with pytest.raises(pydantic.ValidationError, match="扁平价表"):
        AifixConfig()


def test_price_map_accepts_flat_format(monkeypatch):
    monkeypatch.setenv("AIFIX_PRICE_MAP", '{"deepseek-v4-pro": [3.0, 6.0]}')
    assert AifixConfig().price_map["deepseek-v4-pro"] == [3.0, 6.0]


def test_validation_errors_never_echo_the_api_key(monkeypatch):
    """配置校验失败时，报错里不许出现 api_key —— 哪怕只是前缀。

    真实踩到的：跑评测时 source 了整份 .env，里面的 HARNESS_* 被嵌套的
    HarnessConfig（它的 env_prefix 正是 HARNESS_）一并吸走，其中一个值的格式
    对不上，当场 ValidationError —— 而 pydantic 默认会把出错字段的
    **input_value 整个回显**，那是一个 dict，里面躺着 api_key。

    泄漏量取决于 pydantic 对 repr 的截断长度（实测约前 10 个字符），而那不是
    任何人承诺过的东西。任何一次配置写错都会把这段东西打进 stderr、日志、
    CI 输出 —— 密钥不该有这种出场方式。

    修法是 model_config 里的 hide_input_in_errors=True。
    """
    import pydantic
    canary = "sk-CANARY123456789"
    for route in ("DETECTOR", "FIXER"):
        monkeypatch.setenv(f"AIFIX_{route}__API_KEY", canary)
        monkeypatch.setenv(f"AIFIX_{route}__MODEL", "m")
    # 这一条会被嵌套的 HarnessConfig 读到并解析失败，从而把错误挂在
    # detector / fixer 这一层 —— input_value 于是是整个嵌套 dict
    monkeypatch.setenv("HARNESS_MODEL_PRICE_TIERS_BY_MODEL", "{{ 不是合法 JSON")

    # 必须先确认这一路真的还会炸。第三方哪天改了字段名，构造就会成功，
    # 「没泄漏」的断言随之变成恒真 —— 那时该让这条测试红，而不是静默失效。
    with pytest.raises(pydantic.ValidationError) as excinfo:
        AifixConfig()
    text = str(excinfo.value)
    assert canary not in text, text[:400]
    assert canary[:9] not in text, text[:400]   # 连前缀也不许
