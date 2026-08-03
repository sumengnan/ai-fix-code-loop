"""`edit_file`：把一段**原样的文本**换成另一段，不写 diff、不数行号。

为什么要有这个工具（2026-07-31，qwen3-coder-flash 的 39 任务评测）：

    apply_patch 调用 332 次，失败 309 次（93%）
    坏补丁成因：247/247 是 `@@ -a,b +c,d @@` 的行数与正文对不上（100%）

`repair_diff` 把这 97% 的语法故障消掉了，但它治的是症状：unified diff 要求
模型**精确复述上下文行**并且**数对行数**，而数数正是 LLM 结构性最弱的能力。
补丁格式本身就是个坏接口 —— 它把「我要把这段改成那段」这件事，编码成了一道
算术题。

edit_file 换一条路：给出原文、给出新文，剩下的由确定性代码去定位。没有行号，
没有计数，没有上下文行前缀。**能算错的东西不存在，也就没有算错的可能。**

apply_patch 保留不动 —— 新建文件、跨越大段的重排，diff 仍然是对的表达。
"""
import pytest

from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolError

from aifix.signals import under_dirs
from aifix.tools.edit import EditFileTool


def _dirs(dirs):
    """目录列表 → `ProjectAdapter.is_test_path` 那种谓词。

    守卫从「收目录列表」改成「收谓词」（为了 vitest 的同目录布局）之后，
    这些用例各自在考的判断没有变。**逐个包、不统一换成
    `PytestAdapter().is_test_path`**：那会把只给 `["tests"]` 的用例悄悄放宽
    成 `["tests", "test"]`，考的东西被改掉了而测试照样绿。
    """
    return lambda p: under_dirs(p, dirs)


_SRC = '''def most_expensive(items):
    """返回最贵的那件。"""
    if not items:
        return None
    best = items[0]
    for it in items:
        if it.price > best.price:
            best = it
    return best
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text(_SRC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cart.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


def _tool(root, touched=None):
    return EditFileTool(LocalSandbox(workspace=str(root)),
                        is_test=_dirs(["tests"]), touched=touched)


async def _edit(root, touched=None, **kw):
    t = _tool(root, touched)
    return await t.run(t.Params(**kw))


async def test_a_plain_replacement_lands(repo):
    """最基本的一条：原文唯一 → 换掉，文件真的变了。"""
    await _edit(repo, path="src/cart.py",
                old_text="if it.price > best.price:",
                new_text="if it.price >= best.price:")
    assert ">= best.price" in (repo / "src" / "cart.py").read_text()


async def test_multi_line_replacement_keeps_the_rest_byte_identical(repo):
    """替换是**外科式**的：动的只有那一段，别处一个字节都不许变。

    这正是相对 diff 的优势 —— diff 打歪了会连带改到上下文行，而这里替换的
    边界由原文本身给定。
    """
    await _edit(repo, path="src/cart.py",
                old_text="    if not items:\n        return None\n",
                new_text="    if not items:\n        raise ValueError('empty')\n")
    out = (repo / "src" / "cart.py").read_text()
    assert out.startswith('def most_expensive(items):\n    """返回最贵的那件。"""\n')
    assert out.endswith("    return best\n")
    assert "raise ValueError('empty')" in out


async def test_touched_records_the_path_for_delivery(repo):
    """交付靠 `git add -- <paths>`，改了不记账 = 分支上没有这次改动。

    apply_patch 吃过这个亏（记成 `a/calc.py`，交付分支一个提交都没有，报告
    照写「已修复」）。第二条写入路径必须走同一套记账。
    """
    touched: set[str] = set()
    await _edit(repo, touched=touched, path="src/cart.py",
                old_text="best = items[0]", new_text="best = items[-1]")
    assert touched == {"src/cart.py"}


# ── 守卫：第二条写入路径不能是围栏上的一个洞 ────────────────────────────

async def test_test_files_are_refused(repo):
    """**最重要的一条。** 让测试通过的唯一正确方式是改源码。

    新增一个能写文件的工具，如果它不查这一条，整个「不能改测试」的承诺就
    从此作废 —— 而且是静默作废：报告仍然显示绿。
    """
    with pytest.raises(ToolError, match="测试文件"):
        await _edit(repo, path="tests/test_cart.py",
                    old_text="assert True", new_text="assert False")


async def test_escaping_the_workspace_is_refused(repo):
    with pytest.raises(ToolError):
        await _edit(repo, path="../outside.py", old_text="a", new_text="b")


async def test_the_git_directory_is_refused(repo):
    (repo / ".git").mkdir(exist_ok=True)
    (repo / ".git" / "config").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match=".git"):
        await _edit(repo, path=".git/config", old_text="x = 1", new_text="x = 2")


# ── 报错必须可操作：说清楚是哪一种失败、下一步做什么 ──────────────────

async def test_a_missing_old_text_reports_what_is_actually_there(repo):
    """找不到原文时，**把真实的那几行给它**。

    「没找到」是一句死路：模型只能再猜一遍。而我们手上有文件，直接把首行
    命中的位置连内容一起还回去，它下一步就能改对。这是 repair_diff 那条
    教训的推广 —— 报错要指向能走通的路。
    """
    with pytest.raises(ToolError) as e:
        await _edit(repo, path="src/cart.py",
                    old_text="if it.price > best.cost:", new_text="pass")
    msg = str(e.value)
    assert "if it.price > best.price:" in msg, msg


async def test_an_indentation_only_mismatch_is_named_as_such(repo):
    """缩进对不上是**最常见**的一类，而且它长得和「文件里根本没这段」一样。

    我们不替模型猜缩进 —— 猜错就是写进去一段坏代码，而且是静默的。
    只报告「有一处只差缩进」，把原样的行给它，让它重写。
    """
    with pytest.raises(ToolError) as e:
        await _edit(repo, path="src/cart.py",
                    old_text="best = items[0]\nfor it in items:",
                    new_text="best = items[-1]\nfor it in items:")
    assert "缩进" in str(e.value), str(e.value)


async def test_trailing_whitespace_is_tolerated_silently(repo):
    """行尾空白不算差异 —— 它在传输、复制、渲染的任一环都可能被吃掉，
    而它对代码语义**没有任何影响**。这一类不该占用模型一个回合。"""
    await _edit(repo, path="src/cart.py",
                old_text="    best = items[0]   \n    for it in items:   \n",
                new_text="    best = items[-1]\n    for it in items:\n")
    assert "best = items[-1]" in (repo / "src" / "cart.py").read_text()


async def test_an_ambiguous_old_text_is_refused_with_the_line_numbers(repo):
    """出现多次就必须拒绝：随便挑一处改，改错了没人知道。

    报错要带上**每一处的行号**，模型才知道该往 old_text 里补哪段上下文。
    """
    (repo / "src" / "dup.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 1\n", encoding="utf-8")
    with pytest.raises(ToolError) as e:
        await _edit(repo, path="src/dup.py",
                    old_text="    return 1\n", new_text="    return 2\n")
    msg = str(e.value)
    assert "2" in msg and "6" in msg, msg


async def test_replace_all_makes_the_ambiguous_case_explicit(repo):
    """有意改全部时给一个出口 —— 但必须是**显式**的，不能是默认。"""
    (repo / "src" / "dup.py").write_text(
        "x = 1\ny = 1\n", encoding="utf-8")
    out = await _edit(repo, path="src/dup.py", old_text="= 1", new_text="= 2",
                      replace_all=True)
    assert (repo / "src" / "dup.py").read_text() == "x = 2\ny = 2\n"
    assert "2 处" in out


async def test_a_noop_edit_is_refused(repo):
    """old == new 是模型走神了。让它静默成功的话，它会以为自己改了东西，
    然后困惑于测试为什么没变绿 —— 而真正的原因永远不会出现在任何输出里。"""
    with pytest.raises(ToolError, match="完全相同"):
        await _edit(repo, path="src/cart.py",
                    old_text="best = items[0]", new_text="best = items[0]")


async def test_a_missing_file_says_so_plainly(repo):
    with pytest.raises(ToolError, match="不存在"):
        await _edit(repo, path="src/nope.py", old_text="a", new_text="b")


# ── 新建文件 ────────────────────────────────────────────────────────────

async def test_an_empty_old_text_creates_the_file(repo):
    """空 old_text = 新建。比 `--- /dev/null` 那套 diff 写法好记得多。"""
    await _edit(repo, path="src/new_mod.py", old_text="",
                new_text="VALUE = 42\n")
    assert (repo / "src" / "new_mod.py").read_text() == "VALUE = 42\n"


async def test_an_empty_old_text_on_an_existing_file_is_refused(repo):
    """已存在还传空 old_text，意图是「整份覆写」还是「新建」分不出来。
    分不出来就不做 —— 覆写掉一个有内容的文件是不可逆的。"""
    with pytest.raises(ToolError, match="已存在"):
        await _edit(repo, path="src/cart.py", old_text="", new_text="whatever")


async def test_creating_a_test_file_is_still_refused(repo):
    """新建这条路也得过守卫 —— 否则「不能改测试」变成「不能改**已有的**
    测试」，模型新写一个全绿的测试文件就能绕过去。"""
    with pytest.raises(ToolError, match="测试文件"):
        await _edit(repo, path="tests/test_new.py", old_text="",
                    new_text="def test_ok():\n    assert True\n")


# ── 静默损坏：两条都不会报错，只会写坏文件 ──────────────────────────────

async def test_a_binary_file_is_refused_not_mangled(repo):
    """读不动就不改。

    read_file 那边用 `errors="replace"` 是对的 —— 它只是显示。这边要**写
    回去**：把无法解码的字节换成 U+FFFD 再整份写回，等于悄悄损坏一个二进制
    文件，而且没有任何输出会提到这件事。
    """
    (repo / "src" / "blob.bin").write_bytes(b"\x00\xff\xfe\x01binary")
    with pytest.raises(ToolError, match="UTF-8"):
        await _edit(repo, path="src/blob.bin", old_text="binary",
                    new_text="text")
    assert (repo / "src" / "blob.bin").read_bytes().startswith(b"\x00\xff")


async def test_crlf_line_endings_survive_a_loose_match(repo):
    """CRLF 文件不能被改成混行尾。

    宽松匹配走的是拆行重组：`split("\\n")` 留下的每行尾部还挂着 `\\r`，而模型
    写的 new_text 不会有。不补回去，被替换的那几行就变成 LF，整个文件混行尾
    —— 而这既不报错，也不出现在任何回执里。
    """
    p = repo / "src" / "crlf.py"
    p.write_bytes(b"def f():\r\n    return 1   \r\n    return 2\r\n")
    await _edit(repo, path="src/crlf.py",
                old_text="    return 1\n", new_text="    return 9\n")
    raw = p.read_bytes()
    assert b"    return 9\r\n" in raw, raw
    assert b"\n\n" not in raw.replace(b"\r\n", b"")   # 没有裸 LF 混进来


async def test_the_result_says_where_the_change_landed(repo):
    """回执要带**行号**：模型据此判断改对了地方，也据此决定下一步读哪里。"""
    out = await _edit(repo, path="src/cart.py",
                      old_text="best = items[0]", new_text="best = items[-1]")
    assert "src/cart.py" in out and "5" in out, out
