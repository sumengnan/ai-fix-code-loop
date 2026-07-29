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


def test_source_suffixes_is_python_only():
    """挖任务时「哪些后缀算源文件」由适配器回答，不是写死在 mine 里。

    只认 `.py` 是这个适配器的**正确答案**，不是缺口 —— 缺口在于以前它被写死
    在 eval/mine.split_paths 里，于是对 Java 仓库也只认 `.py`。
    """
    assert PytestAdapter().source_suffixes() == (".py",)


def test_test_selectors_are_the_paths_themselves_minus_the_fixtures():
    """pytest 侧「改动过的测试文件路径 → scoped 命令认得的选择器」是恒等映射。

    这一条是**回归钉**：把这件事从 eval/mine 挪进适配器时，pytest 侧的行为
    必须逐点不变。只测 Maven 的话，一个顺手把 pytest 也改坏的实现（比如返回
    类名、或把夹具一并放行）照样能过 Maven 那几条。

    夹具（测试目录下的非 `.py`）必须被丢掉：它们跟着测试进 test_files 是为了
    被 materialize 嫁接，但出现在 pytest 命令行上会让收集整轮中止。
    """
    got = PytestAdapter().test_selectors(
        ["tests/test_calc.py", "tests/data/golden.json", "conftest.py",
         "tests/fixtures/x.sql"])
    assert got == ["tests/test_calc.py", "conftest.py"], got


def test_test_selectors_is_empty_when_only_fixtures_changed():
    """全是夹具时返回空 —— verify_commit 靠这个空值在 materialize 之前收手。"""
    assert PytestAdapter().test_selectors(["tests/data/golden.json"]) == []


def test_file_level_ids_are_the_ones_without_a_node_separator():
    """收集错误产出的 id 就是文件路径本身，用例 id 带 `::`。

    这条判定过去写死在 eval/mine 里（`"::" not in i`），而 `::` 是 pytest
    的语法。回归钉：搬进适配器之后 pytest 侧必须逐点不变。
    """
    a = PytestAdapter()
    assert a.is_file_level_id("tests/test_x.py") is True
    assert a.is_file_level_id("tests/test_x.py::test_a") is False
    assert a.is_file_level_id("tests/test_x.py::TestBar::test_a") is False


def test_cases_under_a_file_id_are_matched_by_the_node_separator():
    """`tests/test_x.py` 名下的用例，而不是碰巧同前缀的另一个文件。

    裸 startswith 会把 `tests/test_xyz.py::t` 也算进来 —— 那个文件红着，
    这个文件就永远判不出「整体变绿」。
    """
    a = PytestAdapter()
    ids = frozenset({"tests/test_x.py::test_a", "tests/test_x.py::test_b",
                     "tests/test_xyz.py::test_c", "tests/test_x.py"})
    assert a.cases_under("tests/test_x.py", ids) == {
        "tests/test_x.py::test_a", "tests/test_x.py::test_b"}
    assert a.cases_under("tests/test_none.py", ids) == set()


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

    理由**不是**「会被扫进交付分支」：Worktree.commit 只
    `git add -- <ApplyPatchTool 记账过的路径>`，这个仓库里根本没有
    `git add -A` 这条交付路径（tests/test_maven_e2e.py 里那条真跑 mvn 的
    验收：交付分支的树上只有 pom.xml 和两个 .java，整个 target/ 都没进去）。
    照那个理由 review 会得出「交付侧会过滤，-B 可以去掉」。

    真实理由是未跟踪产物**跨状态存活**：同一个 worktree 会被
    `git checkout --force` 在 C^ 和 C 之间来回切，而 checkout 不碰未跟踪
    文件，上一跑留下的东西原样活到下一跑 —— 陈旧报告被下一跑当成自己的结果
    就是这个机制。压根不写出来的产物，不需要任何人记得去清。
    见 adapters/pytest_adapter._BASE 上方的说明。
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
