"""`aifix reproduce`：只到复现为止，不修任何东西。

这是 M6 里第一个能给出真实读数的地方——把已修过的 bug 的 commit message 当
issue 正文喂进去，看模型写出来的测试对不对。
"""
import json

import pytest
from harness.llm.base import StreamChunk
from harness.usage import Usage

from aifix.cli import _cmd_reproduce, build_parser

_GOOD = json.dumps({
    "can_reproduce": True,
    "test_file": "tests/test_issue_1.py",
    # 在 buggy_repo 上真的会红：add 少算了，返回 -1 而不是 5
    "test_code": "from calc import add\n\n\ndef test_sum():\n    assert add(2, 3) == 5\n",
    "target_test_id": "tests/test_issue_1.py::test_sum",
    "missing_info": [],
}, ensure_ascii=False)

_GREEN = json.dumps({
    "can_reproduce": True,
    "test_file": "tests/test_issue_2.py",
    # 在 buggy_repo 上是绿的：add(0,0) 无论加减都是 0
    "test_code": "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n",
    "target_test_id": "tests/test_issue_2.py::test_zero",
    "missing_info": [],
}, ensure_ascii=False)

_GIVE_UP = json.dumps({
    "can_reproduce": False, "missing_info": ["没说触发的输入"],
}, ensure_ascii=False)


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _args(repo, issue_file, **over):
    argv = ["reproduce", str(repo), "--issue-text", str(issue_file)]
    for k, v in over.items():
        argv += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
    return build_parser().parse_args(argv)


@pytest.fixture
def issue_file(tmp_path):
    p = tmp_path / "issue.md"
    p.write_text("add 算错了\n\nadd(2, 3) 返回 -1，期望 5。\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- 参数面

def test_issue_text_is_required():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["reproduce", "/tmp/x"])
    assert e.value.code == 2


def test_repo_defaults_to_cwd(tmp_path):
    a = build_parser().parse_args(["reproduce", "--issue-text", str(tmp_path)])
    assert a.repo == "."


def test_first_line_becomes_the_title(buggy_repo, issue_file, capsys):
    """照着 git commit message 的形状读：首行是主题，其余是正文。

    这不是随便定的——任务 3 的验收方式就是把真实 commit message 当 issue
    喂进来，那些文本本来就是这个形状。
    """
    with pytest.raises(SystemExit):
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GOOD)]))
    out = capsys.readouterr().out
    assert "add 算错了" in out


# ---------------------------------------------------------------- 退出码

def test_exit_zero_when_reproduced_and_red(buggy_repo, issue_file, capsys):
    with pytest.raises(SystemExit) as e:
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GOOD)]))
    assert e.value.code == 0
    assert "红检通过" in capsys.readouterr().out


def test_exit_one_when_the_test_is_green(buggy_repo, issue_file, capsys):
    """写出来了但在当前代码上是绿的 —— 没有复现，退非 0。

    反向对照：上一个用例同样「写出来了」却退 0，两者的差别只在红不红。
    """
    with pytest.raises(SystemExit) as e:
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GREEN)]))
    assert e.value.code == 1
    assert "没有失败" in capsys.readouterr().out


def test_exit_one_when_the_model_gives_up(buggy_repo, issue_file, capsys):
    """信息不足时退非 0，但输出里要有可读的缺失清单。

    退出码语义与 `aifix issue handle` **刻意不同**：那边「写不出复现」是一条
    正常结论、退 0；这边是个诊断命令，它问的问题就是「能不能复现」，退出码
    就该回答那个问题。两个命令问的不是同一件事。
    """
    with pytest.raises(SystemExit) as e:
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GIVE_UP)]))
    assert e.value.code == 1
    assert "没说触发的输入" in capsys.readouterr().out


# ---------------------------------------------------------------- 不留痕

def test_the_written_test_is_removed_by_default(buggy_repo, issue_file):
    """诊断命令不该改用户的仓库。红检必须让文件真的落盘才跑得起来，
    所以跑完要收拾干净。"""
    with pytest.raises(SystemExit):
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GOOD)]))
    assert not (buggy_repo / "tests" / "test_issue_1.py").exists()


def test_keep_leaves_the_file_behind(buggy_repo, issue_file):
    with pytest.raises(SystemExit):
        _cmd_reproduce(_args(buggy_repo, issue_file, keep=True),
                       client=_Scripted([_text(_GOOD)]))
    assert (buggy_repo / "tests" / "test_issue_1.py").exists()


def test_refuses_to_overwrite_an_existing_file(buggy_repo, issue_file, capsys):
    """模型给出的路径撞上已有文件时必须停手。

    覆盖掉一个真实的测试文件，代价不是「文件没了」——git 里还有；是那一整个
    文件的用例在这次 baseline 里消失了，而它们的失败本该被计入。
    """
    victim = buggy_repo / "tests" / "test_issue_1.py"
    victim.write_text("# 别人的文件\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _cmd_reproduce(_args(buggy_repo, issue_file),
                       client=_Scripted([_text(_GOOD)]))
    assert e.value.code == 1
    assert "已存在" in capsys.readouterr().out
    assert victim.read_text(encoding="utf-8") == "# 别人的文件\n"


def test_nonexistent_issue_text_gives_human_message_not_traceback(buggy_repo, capsys):
    """--issue-text 指向不存在的文件时，应给出一句能看懂的话并退出码 1，
    而不是甩出 FileNotFoundError 调用栈。

    参照 _cmd_issue 对缺少事件载荷的处理。
    """
    nonexistent = buggy_repo / "does_not_exist.md"
    with pytest.raises(SystemExit) as e:
        _cmd_reproduce(_args(buggy_repo, nonexistent))
    assert e.value.code == 1
    captured = capsys.readouterr()
    assert "does_not_exist.md" in captured.out or "does_not_exist.md" in captured.err
    assert "--issue-text" in captured.out or "--issue-text" in captured.err
