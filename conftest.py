"""项目根 conftest —— 夹具在此对 tests/ 下所有模块可见。"""
import pytest


@pytest.fixture
def issue_file(tmp_path):
    p = tmp_path / "issue.md"
    p.write_text("add 算错了\n\nadd(2, 3) 返回 -1，期望 5。\n", encoding="utf-8")
    return p
