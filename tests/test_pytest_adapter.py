import inspect
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from aifix.adapters.base import Failure
from aifix.adapters.junit import parse_junit
from aifix.adapters.pytest_adapter import PytestAdapter


def test_detect_by_pytest_ini(buggy_repo):
    assert PytestAdapter.detect(buggy_repo) is True


def test_detect_rejects_plain_dir(tmp_path):
    assert PytestAdapter.detect(tmp_path) is False


def test_full_command_includes_junitxml():
    a = PytestAdapter()
    cmd = a.full_test_command()
    assert f"--junitxml={a.REPORT_NAME}" in cmd
    assert "pytest" in cmd


def test_scoped_command_contains_ids():
    a = PytestAdapter()
    cmd = a.scoped_test_command(["tests/test_calc.py::test_add"])
    assert "tests/test_calc.py::test_add" in cmd
    assert f"--junitxml={a.SCOPED_REPORT_NAME}" in cmd


def test_commands_no_longer_take_a_report_path():
    """报告位置是适配器的属性，不是调用方的参数 —— Maven 不接受这个参数。"""
    a = PytestAdapter()
    assert inspect.signature(a.full_test_command).parameters == {}
    assert list(inspect.signature(a.scoped_test_command).parameters) == ["test_ids"]


def test_report_paths_returns_a_list(tmp_path):
    """pytest 只有一份报告，但接口必须是列表 —— Maven surefire 每个测试类一份。"""
    a = PytestAdapter()
    (tmp_path / a.REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
    assert a.report_paths(tmp_path) == [tmp_path / a.REPORT_NAME]


def test_report_paths_is_empty_when_nothing_was_written(tmp_path):
    """报告缺失返回空列表，不是抛 —— require_report 那一层才负责判断。"""
    assert PytestAdapter().report_paths(tmp_path) == []


def test_scoped_report_is_a_different_file_from_the_full_one(tmp_path):
    """两份报告必须分得开：复跑不能覆盖全量那份，否则全量结果被悄悄换掉。"""
    a = PytestAdapter()
    assert a.SCOPED_REPORT_NAME != a.REPORT_NAME
    (tmp_path / a.REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
    # 只有全量那份在：scoped 视角必须看不见它
    assert a.report_paths(tmp_path, scoped=True) == []
    (tmp_path / a.SCOPED_REPORT_NAME).write_text("<testsuites/>", encoding="utf-8")
    assert a.report_paths(tmp_path, scoped=True) == [tmp_path / a.SCOPED_REPORT_NAME]
    assert a.report_paths(tmp_path) == [tmp_path / a.REPORT_NAME]


def test_make_test_id_prefers_file_path():
    """报告里的 classname 是点分模块名，重跑要的是文件路径形式。"""
    tid = PytestAdapter().make_test_id(
        "tests.test_calc", "test_add", "tests/test_calc.py")
    assert tid == "tests/test_calc.py::test_add"


def test_make_test_id_falls_back_to_classname():
    tid = PytestAdapter().make_test_id("tests.test_calc", "test_add", None)
    assert tid == "tests/test_calc.py::test_add"


def test_test_dirs():
    assert "tests" in PytestAdapter().test_dirs()


def test_locate_source_picks_deepest_repo_frame(buggy_repo):
    trace = (
        'Traceback (most recent call last):\n'
        f'  File "{buggy_repo}/tests/test_calc.py", line 5, in test_add\n'
        '    assert add(2, 3) == 5\n'
        f'  File "{buggy_repo}/calc.py", line 2, in add\n'
        '    return a - b\n'
        '  File "/usr/lib/python3.13/site-packages/_pytest/x.py", line 1, in run\n'
    )
    fail = Failure(test_id="t", classname="c", name="n", message="m", trace=trace)
    cands = PytestAdapter().locate_source(fail, buggy_repo)
    assert cands[0].path == "calc.py"        # 最深的 repo 内帧
    assert cands[0].line == 2
    assert cands[0].frame == "add"
    assert all("site-packages" not in c.path for c in cands)


def test_locate_source_empty_when_no_repo_frames(buggy_repo):
    fail = Failure(test_id="t", classname="c", name="n", message="m",
                   trace='File "/usr/lib/python3.13/os.py", line 1, in x\n')
    assert PytestAdapter().locate_source(fail, buggy_repo) == []


def test_commands_disable_bytecode_writing():
    """python -B：不生成 __pycache__。

    否则 Worktree.commit() 的 git add -A 会把 .pyc 扫进交付分支 ——
    真实运行中确实发生了，用户 review 时看到二进制垃圾。
    """
    a = PytestAdapter()
    assert "-B" in a.full_test_command()
    assert "-B" in a.scoped_test_command(["t.py::x"])


_SAMPLE = '''
import pytest

def test_top_fails():
    assert 1 == 2

class TestBar:
    def test_in_class_fails(self):
        assert 1 == 2
    def test_in_class_ok(self):
        pass

@pytest.mark.skip(reason="故意跳过")
def test_skipped():
    pass
'''


def _run_pytest(cwd, args):
    # args 已经是 full_test_command/scoped_test_command 的返回值，其首元素
    # 就是 sys.executable —— 不能再拼一次，否则 python 会把 python 解释器
    # 本身当脚本执行，直接语法报错，r.xml 根本不会被写出来。
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def test_junit_report_carries_file_attribute(tmp_path):
    """哨兵：适配器依赖 <testcase file=...> 存在。pytest 哪天不写了就红。

    不手写 XML —— 手写的只能证明我们理解得自洽，证明不了 pytest 真这么写。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_s.py").write_text(_SAMPLE, encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command())
    cases = list(ET.parse(a.report_paths(tmp_path)[0]).getroot().iter("testcase"))
    assert cases, "pytest 没产出任何 testcase"
    assert all(c.get("file") for c in cases), \
        f"有 testcase 缺 file 属性：{[dict(c.attrib) for c in cases]}"


def test_class_based_test_id_is_runnable(tmp_path):
    """类内测试合成出的 id 必须能被 pytest 真正跑起来。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_s.py").write_text(_SAMPLE, encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command())
    fs = parse_junit(a.report_paths(tmp_path), a.make_test_id)
    tid = "tests/test_s.py::TestBar::test_in_class_fails"
    assert tid in fs.ids, f"合成的 id 不对：{sorted(fs.ids)}"
    # 真跑一次：无效 id 会让 pytest 在收集阶段整轮中止
    res = _run_pytest(tmp_path, a.scoped_test_command([tid]))
    root = ET.parse(a.report_paths(tmp_path, scoped=True)[0]).getroot()
    suite = next(root.iter("testsuite"))
    assert suite.get("tests") == "1", \
        f"pytest 没跑到这个用例：{dict(suite.attrib)}\n{res.stdout}"


def test_collection_error_id_is_the_file_path(tmp_path):
    """收集错误：classname 为空、name 是点分模块名，id 必须退回文件路径。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text(
        "from nonexistent_module import thing\n"
        "def test_x(): assert thing()\n", encoding="utf-8")
    a = PytestAdapter()
    _run_pytest(tmp_path, a.full_test_command())
    fs = parse_junit(a.report_paths(tmp_path), a.make_test_id)
    assert fs.ids == {"tests/test_broken.py"}, sorted(fs.ids)
    # 这个 id 必须可重跑
    res = _run_pytest(tmp_path, a.scoped_test_command(["tests/test_broken.py"]))
    assert "ERROR" in res.stdout or "error" in res.stdout.lower()
    assert a.report_paths(tmp_path, scoped=True) == [
        tmp_path / a.SCOPED_REPORT_NAME]


def test_make_test_id_without_file_strips_class_segments():
    """回退路径（file 缺失，如别的适配器）：尾部大写段当类名，不整段替换。"""
    a = PytestAdapter()
    assert a.make_test_id("tests.test_foo", "test_top", None) == \
        "tests/test_foo.py::test_top"
    assert a.make_test_id("tests.test_foo.TestBar", "test_baz", None) == \
        "tests/test_foo.py::TestBar::test_baz"
    assert a.make_test_id("tests.test_foo.TestOuter.TestInner", "t", None) == \
        "tests/test_foo.py::TestOuter::TestInner::t"
