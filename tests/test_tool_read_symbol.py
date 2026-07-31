"""`read_symbol`：按名字读一个函数/类的**完整**定义。

为什么需要它（2026-07-31 那次评测的两个读数）：

    read_file  431 次
    apply_patch 332 次 / 失败 309 次

模型知道要改哪个函数，却要靠 `grep 拿行号 → read_file 猜一段 → 发现切在
一半 → 再 read_file` 才能把它看全。`read_file` 加了 offset 之后至少**能**读
到尾巴，但每次仍然是「猜一个窗口」——猜小了截断、猜大了灌进一堆无关代码，
把上下文预算烧在与缺陷无关的行上。

函数的边界是**确定的**：Python 有 ast，花括号语言数括号就行。这件事没有理由
让模型去猜。一次调用给出准确的起止行，是省掉的那几个回合，也是省下来的
那几万 token。
"""
import subprocess

import pytest
from harness.sandbox.local import LocalSandbox

from aifix.tools.read_symbol import ReadSymbolTool

_CART = '''import functools


@functools.lru_cache
def most_expensive(items):
    """返回最贵的那件。"""
    best = items[0]
    for it in items:
        if it.price > best.price:
            best = it
    return best


class Cart:
    def __init__(self):
        self.items = []

    def total(self):
        return sum(i.price for i in self.items)


def unrelated():
    return 0
'''

_OTHER = '''def most_expensive(rows):
    return max(rows)
'''

_JAVA = """package demo;

public class Calc {
    public int add(int a, int b) {
        if (a > 0) {
            return a + b;
        }
        return b;
    }

    public int sub(int a, int b) {
        return a - b;
    }
}
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text(_CART, encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text(_OTHER, encoding="utf-8")
    (tmp_path / "src" / "Calc.java").write_text(_JAVA, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


async def _read(root, max_chars=8000, **kw):
    t = ReadSymbolTool(LocalSandbox(workspace=str(root)), max_chars=max_chars)
    return await t.run(t.Params(**kw))


async def test_a_function_comes_back_whole(repo):
    """核心诉求：一次调用拿到**完整**的函数，头尾都不缺。"""
    out = await _read(repo, name="most_expensive", path="src/cart.py")
    assert "def most_expensive(items):" in out
    assert "return best" in out
    # 边界要准：下一个定义不该被卷进来
    assert "class Cart" not in out


async def test_decorators_are_part_of_the_definition(repo):
    """装饰器改变函数的行为，漏掉它读到的就不是这个函数。

    ast 的 `node.lineno` 指向 `def` 那一行，装饰器在它**上面** —— 直接用
    lineno 会静默地切掉 `@lru_cache`，而缓存恰恰是这类缺陷的常见成因。
    """
    out = await _read(repo, name="most_expensive", path="src/cart.py")
    assert "@functools.lru_cache" in out


async def test_line_numbers_are_real_file_line_numbers(repo):
    """行号必须是**文件里的**行号，不是片段内的偏移。

    模型要拿它去 read_file 续读、去核对 grep 的结果。从 1 开始重新编号的话
    这两件事都会指向错误的位置，而且错得很安静。
    """
    out = await _read(repo, name="most_expensive", path="src/cart.py")
    # 装饰器在第 4 行（import / 空 / 空 / @）
    assert "     4\t@functools.lru_cache" in out, out


async def test_a_method_is_found_by_its_dotted_name(repo):
    """`Cart.total` 这种写法要认 —— 同名方法在一个仓库里很常见，
    只按裸名字找会给回一堆同名的东西。"""
    out = await _read(repo, name="Cart.total", path="src/cart.py")
    assert "def total(self):" in out
    assert "def __init__" not in out


async def test_a_class_comes_back_with_its_methods(repo):
    out = await _read(repo, name="Cart", path="src/cart.py")
    assert "def __init__" in out and "def total" in out
    assert "def unrelated" not in out


async def test_the_file_is_found_without_being_told(repo):
    """不给 path 也要能用 —— 「我知道函数名，不知道它在哪个文件」正是
    这个工具存在的场合。"""
    out = await _read(repo, name="unrelated")
    assert "src/cart.py" in out and "return 0" in out


async def test_same_name_in_several_files_shows_all_of_them(repo):
    """重名时**全都给出来**，并且标明各自的文件。

    随便挑一个返回是最坏的做法：模型会照着改，而它改的可能是另一个。
    """
    out = await _read(repo, name="most_expensive")
    assert "src/cart.py" in out and "src/other.py" in out


async def test_a_java_method_is_bounded_by_braces(repo):
    """花括号语言走另一条路 —— Maven 适配器是一等公民，不能只服务 Python。"""
    out = await _read(repo, name="add", path="src/Calc.java")
    assert "public int add(int a, int b) {" in out
    assert "return a + b;" in out
    # 内层 if 的 `}` 不能提前收尾，而 sub 不该被卷进来
    assert "public int sub" not in out, out


async def test_code_blocks_in_docs_do_not_crowd_out_the_real_thing(repo):
    """文档里的代码块**不算定义**。

    冒烟时撞出来的：查 `EditFileTool.run`，第一个命中的是一份计划文档的
    markdown —— 里面的代码块有 `def run(`，缩进兜底认了它，真正的实现被挤出
    名额。文档里的代码是**过去某个版本**的样子，照着它改是最难查的一类错。
    """
    (repo / "PLAN.md").write_text(
        "# 计划\n\n```python\ndef unrelated():\n    return '文档里的旧版本'\n```\n",
        encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = await _read(repo, name="unrelated")
    assert "src/cart.py" in out
    assert "PLAN.md" not in out, out


async def test_an_exact_dotted_match_outranks_a_bare_one(repo):
    """查 `Cart.total` 时，别处一个裸 `total` 不该把真正的那个挤到后面。"""
    (repo / "src" / "misc.py").write_text(
        "def total():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = await _read(repo, name="Cart.total")
    assert out.index("src/cart.py") < out.index("src/misc.py"), out


async def test_an_unknown_name_says_so_and_suggests_grep(repo):
    """找不到就明说，并指向下一步。含糊的报错会让模型原地重试。"""
    out = await _read(repo, name="no_such_function")
    assert "没找到" in out and "grep" in out


async def test_a_syntax_error_file_degrades_instead_of_crashing(repo):
    """源码在修复过程中处于半坏状态是**常态**（模型刚改了一半）。
    这时候崩掉等于在最需要工具的时刻把工具拿走。
    """
    (repo / "src" / "broken.py").write_text(
        "def f(:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = await _read(repo, name="f", path="src/broken.py")
    assert "def f(:" in out, out          # 退到正则也要把这段给出来


async def test_a_huge_symbol_is_truncated_with_a_way_to_continue(repo):
    """截断必须**可操作** —— 这是 read_file 那条教训，同样适用于这里。"""
    body = "\n".join(f"    x = {i}" for i in range(400))
    (repo / "src" / "big.py").write_text(
        f"def huge():\n{body}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    out = await _read(repo, max_chars=600, name="huge", path="src/big.py")
    assert "offset" in out and "read_file" in out, out[-300:]


async def test_it_refuses_to_leave_the_workspace(repo):
    from harness.tools.base import ToolError

    with pytest.raises(ToolError):
        await _read(repo, name="whatever", path="../../etc/passwd")
