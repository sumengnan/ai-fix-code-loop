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


# 下面三段是 pytest 9.1.1 真正写进 JUnit XML 的 <failure> 文本，逐字复制自
# 一次真跑（2026-07-29，见 docs/adapters.md）。上面那两条用的是手写的
# Python 原生 traceback（`File "...", line N, in fn`）—— 那个格式 pytest 的
# longrepr **从不产出**，于是 _FRAME 在真实数据上一帧都匹配不到，而测试全绿。
# 假输入喂出来的绿灯是这个 bug 能活到现在的唯一原因，所以这几条必须用真数据。

_REAL_PROPAGATED = '''def test_boom():
>       assert outer(0) == 1
               ^^^^^^^^

tests/test_mod.py:6:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
src/mod.py:2: in outer
    return inner(x)
           ^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

x = 0

    def inner(x):
>       return 10 / x
               ^^^^^^
E       ZeroDivisionError: division by zero

src/mod.py:5: ZeroDivisionError'''

_REAL_ASSERTION = '''def test_add():
>       assert add(2, 3) == 5
E       AssertionError: assert -1 == 5
E        +  where -1 = add(2, 3)

tests/test_calc.py:5: AssertionError'''


def _frames(repo, trace):
    fail = Failure(test_id="t", classname="c", name="n", message="m",
                   trace=trace)
    return PytestAdapter().locate_source(fail, repo)


def test_locate_source_parses_the_format_pytest_actually_writes(tmp_path):
    """异常穿过源码时，那几帧必须被认出来——它们是 Detector 唯一的锚点。

    pytest 的格式是 `src/mod.py:2: in outer`（相对 rootdir，冒号分隔），
    不是 Python 原生的 `File "...", line N, in fn`。认不出的后果不是报错，
    是 Detector 拿到一句「未能从栈帧定位到 repo 内的源码」然后盲猜路径。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/mod.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "tests/test_mod.py").write_text("x\n", encoding="utf-8")

    cands = _frames(tmp_path, _REAL_PROPAGATED)
    assert cands, "真实 pytest traceback 一帧都没解出来"
    # 最深的最可疑：ZeroDivisionError 抛出处
    assert (cands[0].path, cands[0].line) == ("src/mod.py", 5)
    assert (cands[1].path, cands[1].line, cands[1].frame) == \
        ("src/mod.py", 2, "outer")
    # 测试文件自己的帧也在，排最后（最浅）
    assert (cands[-1].path, cands[-1].line) == ("tests/test_mod.py", 6)


def test_locate_source_on_a_plain_assertion_yields_the_test_frame(buggy_repo):
    """纯断言失败的 traceback 里**没有**源码帧，只有断言所在的测试文件。

    这不是解析能补回来的信息：被调函数正常返回了，栈上根本没有它。
    能拿到的只有测试文件那一帧，那也得拿到——它至少给出准确的行号。
    """
    cands = _frames(buggy_repo, _REAL_ASSERTION)
    frames = [c for c in cands if c.origin == "traceback"]
    assert [(c.path, c.line) for c in frames] == [("tests/test_calc.py", 5)]


# ======== 无栈帧可锚时，退到测试文件的 import ========

def test_plain_assertion_falls_back_to_what_the_test_imports(buggy_repo):
    """栈上没有源码帧时，去看测试文件 import 了什么——那是真证据，不是猜。

    这条挡的是实测行为：纯断言失败下 Detector 的输入里一个产品代码文件都
    没有（它连仓库有哪些目录都不知道），只能按包名猜路径。同一个失败连跑
    三次给出 `cart.py` / `cart.py` / `src/cart.py`，真实路径是
    `src/shopcart/cart.py`——三次都没对，而按分段后缀判定，前两个算命中、
    第三个算未命中。量到的是运气，不是定位能力。

    `tests/test_calc.py` 顶上写着 `from calc import add`，`calc.py` 就在
    repo 里——这是确定性的、零 LLM 的锚点。
    """
    cands = _frames(buggy_repo, _REAL_ASSERTION)
    imported = [c for c in cands if c.origin == "import"]
    assert [c.path for c in imported] == ["calc.py"]
    # 不止给出文件：被失败点名的符号要定位到它的 def 行，锚点才和栈帧等价
    assert (imported[0].line, imported[0].frame) == (1, "add")
    # 源码候选排在测试文件那一帧前面——「按可疑度排序」，测试文件最不可疑
    assert cands[0].path == "calc.py", [c.path for c in cands]


def test_import_fallback_stays_out_of_the_way_when_frames_exist(tmp_path):
    """栈帧解出来了就别掺 import——那是更弱的证据，会稀释真锚点。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/mod.py").write_text("def outer(x):\n    pass\n",
                                         encoding="utf-8")
    (tmp_path / "tests/test_mod.py").write_text(
        "from src.mod import outer\n", encoding="utf-8")

    cands = _frames(tmp_path, _REAL_PROPAGATED)
    assert cands, "真实 traceback 一帧都没解出来"
    assert all(c.origin == "traceback" for c in cands), \
        [(c.path, c.origin) for c in cands]


def test_import_fallback_drops_stdlib_and_third_party(tmp_path):
    """`import json` 不是锚点。给 Detector 一个错候选比不给更糟。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a\n",
                                      encoding="utf-8")
    (tmp_path / "tests/test_calc.py").write_text(
        "import json\n"
        "import pytest\n"
        "from calc import add\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n", encoding="utf-8")

    trace = ("def test_add():\n"
             ">       assert add(2, 3) == 5\n"
             "E       AssertionError\n\n"
             "tests/test_calc.py:6: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    assert [c.path for c in imported] == ["calc.py"], \
        [c.path for c in imported]


def test_import_fallback_ranks_symbols_the_failure_names_first(tmp_path):
    """一个测试文件 import 五个模块时，排序才是这条路有没有用的关键。

    断言文本里点了名的符号，它所在的模块最可疑——不排序的话
    Detector 拿到的是一份没有次序的清单，跟没有锚点差不了多少。
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "cart.py").write_text(
        "def subtotal(items):\n    return 0\n\n\n"
        "def most_expensive(items, n):\n    return []\n", encoding="utf-8")
    (tmp_path / "fmt.py").write_text("def money(x):\n    return x\n",
                                     encoding="utf-8")
    (tmp_path / "tests/test_cart.py").write_text(
        "from fmt import money\n"
        "from cart import subtotal, most_expensive\n\n\n"
        "def test_rank():\n"
        "    assert most_expensive([], 1) == []\n", encoding="utf-8")

    trace = ("def test_rank():\n"
             ">       assert most_expensive([], 1) == []\n"
             "E       AssertionError\n\n"
             "tests/test_cart.py:5: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    assert imported[0].path == "cart.py", [c.path for c in imported]
    # 定位到被点名的那个函数，不是文件里的第一个
    assert (imported[0].line, imported[0].frame) == (5, "most_expensive")


def test_import_fallback_finds_src_layout_packages(tmp_path):
    """`from shopcart.cart import x` → `src/shopcart/cart.py`。

    src 布局下模块路径和仓库路径差一段前缀，裸拼 `repo / 模块路径` 找不到。
    这正是实测里模型猜 `cart.py` / `src/cart.py` 都没猜中的那段前缀。
    """
    (tmp_path / "src/shopcart").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/shopcart/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src/shopcart/cart.py").write_text(
        "def most_expensive(items, n):\n    return []\n", encoding="utf-8")
    (tmp_path / "tests/test_cart.py").write_text(
        "from shopcart.cart import most_expensive\n", encoding="utf-8")

    trace = ("def test_rank():\n"
             ">       assert most_expensive([], 1) == []\n"
             "E       AssertionError\n\n"
             "tests/test_cart.py:5: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    assert [c.path for c in imported] == ["src/shopcart/cart.py"], \
        [c.path for c in imported]


def test_import_fallback_follows_package_re_exports(tmp_path):
    """`from shopcart import x` 落在 `__init__.py` 上等于没定位到。

    这是实测发现的：ai-fix-demo 的测试写的是 `from shopcart import
    most_expensive`，而 `src/shopcart/__init__.py` 只有一行
    `from .cart import ...` 转发，逻辑全在 `src/shopcart/cart.py`。停在
    `__init__.py` 给出的是一个**不含任何逻辑**的文件，比模型自己猜好不了
    多少，gold_files 也对不上。

    包内相对 import 在这里是可解的（与测试文件里的相对 import 不同）：
    `__init__.py` 自己的目录就是包目录，`.cart` 就是同级的 cart.py，
    纯路径运算，不需要猜 rootdir。
    """
    (tmp_path / "src/shopcart").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/shopcart/__init__.py").write_text(
        "from .cart import most_expensive, subtotal\n\n"
        '__all__ = ["most_expensive", "subtotal"]\n', encoding="utf-8")
    (tmp_path / "src/shopcart/cart.py").write_text(
        "def subtotal(items):\n    return 0\n\n\n"
        "def most_expensive(items, n):\n    return []\n", encoding="utf-8")
    (tmp_path / "tests/test_cart.py").write_text(
        "from shopcart import most_expensive\n", encoding="utf-8")

    trace = ("def test_rank():\n"
             ">       assert most_expensive(items, 2) == []\n"
             "E       AssertionError\n\n"
             "tests/test_cart.py:52: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    assert [c.path for c in imported] == ["src/shopcart/cart.py"], \
        [c.path for c in imported]
    assert (imported[0].line, imported[0].frame) == (5, "most_expensive")


def test_import_fallback_survives_a_re_export_cycle(tmp_path):
    """互相转发的两个模块不能把定位转成死循环。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg/__init__.py").write_text(
        "from .a import boom\n", encoding="utf-8")
    (tmp_path / "pkg/a.py").write_text("from .b import boom\n",
                                       encoding="utf-8")
    (tmp_path / "pkg/b.py").write_text("from .a import boom\n",
                                       encoding="utf-8")
    (tmp_path / "tests/test_x.py").write_text("from pkg import boom\n",
                                              encoding="utf-8")

    trace = ("def test_x():\n"
             ">       assert boom() == 1\n"
             "E       AssertionError\n\n"
             "tests/test_x.py:3: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    # 追不到定义，但必须**终止**并给出追到的最后一处
    assert imported and imported[0].path.startswith("pkg/"), \
        [c.path for c in imported]


def test_import_fallback_never_walks_into_venv(tmp_path):
    """仓库里的 `.venv` 有上万个 .py，且它们一个都不是产品代码。"""
    (tmp_path / ".venv/lib/python3.14/site-packages/calc").mkdir(parents=True)
    (tmp_path / ".venv/lib/python3.14/site-packages/calc/__init__.py").write_text(
        "def add(a, b):\n    return a\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_calc.py").write_text(
        "from calc import add\n", encoding="utf-8")

    trace = ("tests/test_calc.py:5: AssertionError")
    imported = [c for c in _frames(tmp_path, trace) if c.origin == "import"]
    assert imported == [], [c.path for c in imported]


def test_locate_source_still_parses_native_tracebacks(buggy_repo):
    """`File "...", line N, in fn` 仍要认：--tb=native 与嵌套异常会产出它。"""
    trace = (f'  File "{buggy_repo}/calc.py", line 2, in add\n'
             '    return a - b\n')
    assert [(c.path, c.line, c.frame) for c in _frames(buggy_repo, trace)] == \
        [("calc.py", 2, "add")]


def test_locate_source_ignores_paths_that_are_not_in_the_repo(tmp_path):
    """行号形状的文本到处都是（日志、字符串字面量、第三方帧）。

    pytest 那种格式没有引号做界，只能靠「这个路径在 repo 里真的存在」收口；
    否则 Detector 的候选列表里会混进根本不存在的文件，比没有候选更误导。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src/mod.py").write_text("x\n", encoding="utf-8")
    trace = ('src/mod.py:2: in outer\n'
             'nowhere/gone.py:9: in missing\n'
             '/usr/lib/python3.13/site-packages/_pytest/x.py:1: in run\n')
    assert [c.path for c in _frames(tmp_path, trace)] == ["src/mod.py"]


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
