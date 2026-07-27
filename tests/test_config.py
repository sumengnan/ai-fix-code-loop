from aifix.config import AifixConfig


def test_defaults():
    c = AifixConfig()
    assert c.max_attempts == 3
    assert c.budget_usd == 2.0
    assert c.budget_tokens == 500_000
    assert c.budget_wall_seconds == 1800.0
    assert c.allow_test_edits is False
    assert c.fixer_max_steps == 25


def test_nested_env_overrides(monkeypatch):
    monkeypatch.setenv("AIFIX_DETECTOR__MODEL", "glm-4.6")
    monkeypatch.setenv("AIFIX_FIXER__MODEL", "deepseek-chat")
    c = AifixConfig()
    assert c.detector.model == "glm-4.6"
    assert c.fixer.model == "deepseek-chat"


def test_scalar_env_override(monkeypatch):
    monkeypatch.setenv("AIFIX_MAX_ATTEMPTS", "5")
    assert AifixConfig().max_attempts == 5
