"""复现测试**绝不覆盖已有文件**，而且这条守卫必须在唯一的写入点上。

实测撞出来的（2026-08-01 的功能巡检）：拿一个真实小仓库跑
`aifix reproduce`，模型给出的路径是 `tests/test_calc.py` —— 仓库里已经有的
那个文件。命令行拒绝了，**而 issue 那条路直接写了下去**。

后果链条，每一环都不报错：

    整个 test_calc.py 被一条生成的测试替换
      → git add + commit 进交付分支
      → baseline 跑起来，原来那些用例**已经不存在了**
      → 「这个补丁没弄坏别的」在一个少了一堆用例的对照组上成立
      → PR 里躺着一次删测试的改动，而报告说一切正常

这不是理论风险：**模型挑中已有测试文件是常态**，不是意外。`tests/test_calc.py`
正是任何人都会给一个 `calc.py` 的缺陷写测试的地方。

守卫放在 `write_reproduction` 里，不放在两个调用方各一份 —— 那正是它当初
只存在于命令行那一侧的原因。
"""
from pathlib import Path

import pytest

from aifix.agents.reproducer import Reproduction
from aifix.reproduce import write_reproduction


def _repro(path="tests/test_calc.py", code="def test_new():\n    assert False\n"):
    return Reproduction(can_reproduce=True, test_file=path, test_code=code,
                        target_test_id=f"{path}::test_new")


def test_an_existing_file_is_never_clobbered(tmp_path):
    """**最要紧的一条。**

    判据是原文一个字节都不能少 —— 而不是「文件还在」：被整份替换之后
    文件当然还在，里面的用例却没了。
    """
    (tmp_path / "tests").mkdir()
    victim = tmp_path / "tests" / "test_calc.py"
    original = "def test_add():\n    assert add(2, 3) == 5\n"
    victim.write_text(original, encoding="utf-8")

    written = write_reproduction(tmp_path, _repro())

    assert victim.read_text(encoding="utf-8") == original, "已有测试被覆盖了"
    assert written != victim, "写入路径不该还是那个已存在的文件"
    assert written.exists() and "test_new" in written.read_text(encoding="utf-8")


def test_the_fallback_stays_inside_the_test_directory(tmp_path):
    """改名之后仍然要落在测试目录下。

    挪出去的话，「不许改测试文件」那道守卫按 test_dirs 判定时不认它 ——
    修复阶段的 agent 就能随手改掉自己的判卷标准。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x = 1\n", encoding="utf-8")
    written = write_reproduction(tmp_path, _repro())
    assert written.parent == tmp_path / "tests"
    assert written.name.startswith("test_"), "pytest 只收集 test_ 开头的文件"


def test_the_returned_path_is_the_one_actually_written(tmp_path):
    """返回值是调用方用来 `git add`、用来在红检失败时收走文件的那个路径。

    返回旧路径的话：交付时 add 的是一个没被改过的文件（分支上什么都没有），
    而红检失败时 unlink 掉的是**用户真实的测试文件**。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x = 1\n", encoding="utf-8")
    written = write_reproduction(tmp_path, _repro())
    assert written.read_text(encoding="utf-8") == "def test_new():\n    assert False\n"


def test_the_target_id_is_rewritten_to_match(tmp_path):
    """**改了文件名就必须改 target_test_id。**

    不改的话写下去的是 A、跑起来的是 B：红检会说「这个用例没跑出结果」，
    而真相是它在另一个文件里。parse_reproduction 里那条「id 要追溯得到
    test_file」的校验，说的正是同一件事。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x = 1\n", encoding="utf-8")
    r = _repro()
    written = write_reproduction(tmp_path, r)
    assert r.test_file == written.relative_to(tmp_path).as_posix()
    assert r.target_test_id.startswith(r.test_file + "::")
    assert r.target_test_id.endswith("::test_new")


def test_a_free_path_is_used_as_given(tmp_path):
    """反向对照：没冲突时**一个字都不许改**。

    否则这个函数在「防覆盖」的同时也在「改好路径」，而后者是纯粹的风险 ——
    模型给的 id 与文件名的对应关系已经被 parse_reproduction 校验过了。
    """
    (tmp_path / "tests").mkdir()
    r = _repro("tests/test_issue_7.py")
    written = write_reproduction(tmp_path, r)
    assert written == tmp_path / "tests" / "test_issue_7.py"
    assert r.test_file == "tests/test_issue_7.py"
    assert r.target_test_id == "tests/test_issue_7.py::test_new"


def test_it_keeps_looking_until_it_finds_a_free_name(tmp_path):
    """连撞几次也要能落地 —— 同一个 issue 反复触发是很正常的用法。"""
    (tmp_path / "tests").mkdir()
    for name in ("test_calc.py", "test_calc_aifix.py", "test_calc_aifix_2.py"):
        (tmp_path / "tests" / name).write_text("x = 1\n", encoding="utf-8")
    written = write_reproduction(tmp_path, _repro())
    assert not written.exists() or written.read_text(encoding="utf-8").startswith(
        "def test_new")
    assert written.name not in ("test_calc.py", "test_calc_aifix.py",
                                "test_calc_aifix_2.py")


def test_subdirectories_are_still_created(tmp_path):
    """模型完全可能给 `tests/regression/test_x.py`，而那个子目录未必存在。"""
    (tmp_path / "tests").mkdir()
    written = write_reproduction(tmp_path, _repro("tests/regression/test_x.py"))
    assert written.exists()
