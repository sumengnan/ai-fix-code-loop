"""复现测试的 `test_code` 必须是一份**自包含的模块**。

实测逼出来的（2026-08-04，ai-learning-helper#89，deepseek-v4-pro）：模型诊断
完全正确、测试语义也完全正确，但它把 `test_file` 指向了仓库里**已有**的
`tests/app/test_url_blocklist.py`，`test_code` 只写了一个函数体，靠那个文件
现成的 `import pytest` / `UrlBlockStore` / `_CountingFetch` 吃饭 —— 它甚至专门
回头读了那个文件的第 1-10 行去核实这些名字在不在。

而 `write_reproduction` **绝不覆盖已有文件**（2026-08-01 的事故守卫），撞名就
改写到全新的 `test_url_blocklist_aifix.py`。于是模型刚核实过的每一个名字瞬间
失效，测试红在 `NameError: name 'UrlBlockStore' is not defined` 上，红检判
no_repro，48k tokens 白烧，而回帖把成因说成「用了 pytest.raises 却没有
import pytest」—— 一句指错方向的诊断。

**改名不是要修的那一头。** 给 `calc.py` 的缺陷写测试、挑中 `tests/test_calc.py`
是任何人都会做的选择，拒绝它等于把常态判成违约。要钉死的是另一头：只要
`test_code` 自包含，改名就无害。这条不变量同时堵住 2026-08-02 issue #9 那次
（`pytest.raises` 没 import，fixer 对着假靶子烧掉 $1.45 / 468k tokens）。
"""
import json

from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.agents.reproducer import parse_reproduction_ex

_A = PytestAdapter()


def _raw(code: str, path: str = "tests/test_issue_89.py", tid: str = "") -> str:
    return json.dumps({
        "can_reproduce": True,
        "test_file": path,
        "test_code": code,
        "target_test_id": tid or f"{path}::test_x",
        "missing_info": [],
    })


def _why(code: str) -> str:
    r, why = parse_reproduction_ex(_raw(code), _A.is_test_path)
    return why


# ------------------------------------------------------------ 要挡住的

def test_rejects_a_fragment_that_leans_on_another_files_imports():
    """#89 的原样形态：函数体引用了本文件里从未绑定过的名字。"""
    why = _why(
        "async def test_placeholder_not_recorded():\n"
        "    store = UrlBlockStore(\":memory:\")\n"
        "    tool = guard_fetch_tool(_CountingFetch(), store)\n"
        "    with pytest.raises(ToolError):\n"
        "        await tool.run(tool.Params(url=\"https://example.com/x\"))\n")
    assert why, "片段式的 test_code 必须被挡下"


def test_the_reason_names_every_missing_name():
    """报告里必须点出**缺的是哪几个名字**。

    只说「不自包含」等于让人自己去比对——而这一步的全部价值就是把下一步动作
    说清楚。反向对照：不能只报第一个就收工。
    """
    why = _why(
        "def test_x():\n"
        "    store = UrlBlockStore()\n"
        "    assert guard_fetch_tool(store) is not None\n")
    assert "UrlBlockStore" in why
    assert "guard_fetch_tool" in why


def test_rejects_the_missing_import_pytest_shape():
    """2026-08-02 issue #9 那次的形态——同一道闸要一并接住。"""
    why = _why("from calc import add\n\n\n"
               "def test_x():\n"
               "    with pytest.raises(ValueError):\n"
               "        add(1, 2)\n")
    assert "pytest" in why


# ------------------------------------------------------------ 不能误伤的
#
# 误报比漏报贵得多：漏掉的还有红检那道闸兜着，而误报会把一条**完全正确**的
# 复现直接打回，且模型无从得知自己错在哪。以下每一条都是合法的测试写法。

def test_accepts_a_self_contained_module():
    assert _why("from calc import add\n\n\n"
                "def test_x():\n    assert add(2, 3) == 5\n") == ""


def test_accepts_names_bound_by_every_ordinary_form():
    """赋值 / 解包 / for / 推导式 / with as / except as / 海象 / 类与函数定义。"""
    assert _why(
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "class Helper:\n"
        "    pass\n"
        "\n"
        "def _make():\n"
        "    return Helper()\n"
        "\n"
        "def test_x(tmp_path):\n"
        "    a, b = 1, 2\n"
        "    rows = [r * a for r in range(b)]\n"
        "    with open(Path(tmp_path) / 'f') as fh:\n"
        "        data = fh.read()\n"
        "    try:\n"
        "        json.loads(data)\n"
        "    except ValueError as exc:\n"
        "        assert exc is not None\n"
        "    if (n := len(rows)) > 0:\n"
        "        assert n == b\n"
        "    assert _make() is not None\n") == ""


def test_accepts_pytest_fixture_arguments():
    """fixture 靠参数名注入，本文件里当然找不到它的定义——不是缺失。"""
    assert _why("def test_x(tmp_path, monkeypatch, capsys):\n"
                "    assert tmp_path is not None\n") == ""


def test_accepts_decorated_and_async_tests():
    assert _why(
        "import pytest\n"
        "\n"
        "@pytest.mark.parametrize('v', [1, 2])\n"
        "async def test_x(v):\n"
        "    assert v\n") == ""


def test_a_star_import_switches_the_check_off():
    """`from x import *` 之后，本文件绑定了什么已经不可知了。

    这时候只有两种选择：假装知道（必然误报），或者放行。按这个项目一贯的判法
    ——「没有证据」不能当成「有罪的证据」——放行。红检那道闸仍在后面。
    """
    assert _why("from helpers import *\n\n\n"
                "def test_x():\n    assert make_thing() is not None\n") == ""


def test_a_global_declaration_counts_as_bound():
    assert _why("def test_x():\n"
                "    global _cache\n"
                "    _cache = 1\n"
                "    assert _cache == 1\n") == ""


def test_module_dunders_are_not_missing_names():
    assert _why("def test_x():\n    assert __name__\n") == ""


def test_the_check_only_applies_to_python_files():
    """**这道闸是 Python AST，只能判 Python。**

    aifix 同时支持 maven（Java）和 vitest（TypeScript）。拿 `ast.parse` 去读
    Java 源码，绝大多数时候会 SyntaxError 然后放行——「碰巧安全」不是安全：
    一段恰好也是合法 Python 的 Java/TS 片段（`x;`、`foo()`）会被判成缺名字，
    而那是一句彻头彻尾的假话。

    这正是注释里记着的那个教训（`::` 是 pytest 的语法，M5 的裂缝 5 就是把它
    当通用格式写死栽的）——按扩展名判，别赌解析器会替我们兜住。
    """
    from aifix.checks.signals import under_dirs

    is_java_test = lambda p: under_dirs(p, ["src/test"])
    raw = _raw("x", path="src/test/java/com/example/FooTest.java",
               tid="com.example.FooTest#testBar")
    r, why = parse_reproduction_ex(raw, is_java_test)
    assert why == "", f"Java 文件不该被 Python AST 判定：{why}"
    assert r is not None


def test_a_syntax_error_is_not_reported_as_a_missing_name():
    """语法错是另一道闸的活（收集阶段就会炸），这里不抢答。

    抢答的代价是把人指向「缺了个名字」，而真相是这份代码根本解析不了。
    """
    assert _why("def test_x(:\n    pass\n") == ""
