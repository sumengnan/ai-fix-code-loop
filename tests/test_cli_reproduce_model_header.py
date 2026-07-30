"""`aifix reproduce` 输出里必须包含模型名。"""

import pytest

from aifix.config import AifixConfig
from aifix.cli import _cmd_reproduce
from tests.test_cli_reproduce import _GOOD, _Scripted, _args, _text


def test_header_includes_model_name(buggy_repo, issue_file, capsys):
    """`aifix reproduce` 的输出抬头里必须有一行模型名。

    这个子命令的全部用途就是量「某个模型能不能写对复现测试」。
    连着跑五次、换着模型跑，输出里却没有东西区分这五份读数出自谁——
    存下来的日志过两天就没法用了。
    """
    model = AifixConfig().fixer.model
    with pytest.raises(SystemExit):
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GOOD)]))
    out = capsys.readouterr().out
    assert f"- 模型：{model}" in out
