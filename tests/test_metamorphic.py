"""变形复跑：这个补丁是只对那一个例子成立，还是真的修好了。

复现测试只有**一个样本点**，而 fixer 的停止条件就是「它绿了」。于是一个贴着
那个样本写的补丁，和一个真修好的补丁，在判定眼里一模一样。

做法是机械地扰动测试里的字面量再跑一遍。扰动**不经模型**——它不需要知道正确
答案，只需要知道「这个变换不该改变结论」。

误伤是这里最贵的错误（滚掉一个本来正确的补丁），所以每个扰动都带对照组：

    变形后的测试在 HEAD（未打补丁）上仍然红  →  这个变形仍然测得到那个缺陷
    变形后的测试在打了补丁之后还是红        →  补丁没泛化

对照组不红，说明扰动把测试本身搞坏了（比如动到的是期望值而不是输入），
这一个扰动直接丢弃，不出声。
"""
import ast

from aifix.metamorphic import plan_mutations


def _seg(source: str, m) -> str:
    return source[m.start:m.end]


def _applied(source: str, m) -> str:
    return source[:m.start] + m.text + source[m.end:]


# ------------------------------------------------------- 扰动的生成

def test_a_constant_list_gets_rotated():
    src = 'def test_x():\n    ids = ["q1", "q2", "q3"]\n    assert ids\n'
    (m,) = plan_mutations(src)
    assert _seg(src, m) == '["q1", "q2", "q3"]'
    assert m.text == '["q3", "q1", "q2"]'
    # 换完还得是能解析的 Python
    ast.parse(_applied(src, m))


def test_element_formatting_survives():
    """按原文片段拼回去，不重新渲染 —— 重新渲染会顺手改写引号、去掉注释，
    让「只换了序」变成一次谁也说不清的改写。"""
    src = "def test_x():\n    v = ['a',   'b']\n"
    (m,) = plan_mutations(src)
    assert m.text == "['b', 'a']"


def test_a_single_element_list_is_not_worth_rotating():
    assert plan_mutations('def test_x():\n    v = ["only"]\n') == []


def test_an_empty_list_is_skipped():
    assert plan_mutations("def test_x():\n    v = []\n") == []


def test_non_constant_elements_are_skipped():
    """元素里有表达式时换序可能改变求值顺序，那就不只是「换个序」了。"""
    assert plan_mutations("def test_x():\n    v = [f(), g()]\n") == []


def test_a_syntax_error_yields_nothing():
    """解析不了就不扰动 —— 这一层不该是发现语法错的地方。"""
    assert plan_mutations("def test_x(:\n    pass\n") == []


def test_every_constant_list_is_offered():
    src = ('def test_x():\n'
           '    a = [1, 2]\n'
           '    b = ["p", "q", "r"]\n')
    ms = plan_mutations(src)
    assert len(ms) == 2
    assert {_seg(src, m) for m in ms} == {"[1, 2]", '["p", "q", "r"]'}


def test_mutations_come_back_in_source_order():
    """按出现顺序返回：上限截断时截掉的该是靠后的那些，而不是随机的一批。"""
    src = "def test_x():\n    a = [1, 2]\n    b = [3, 4]\n    c = [5, 6]\n"
    ms = plan_mutations(src)
    assert [m.start for m in ms] == sorted(m.start for m in ms)


def test_every_mutation_carries_a_readable_label():
    """报告要能说清「动了哪一处」，行号加原文。"""
    src = 'def test_x():\n    ids = ["q1", "q2"]\n'
    (m,) = plan_mutations(src)
    assert "2" in m.label            # 行号
    assert '["q1", "q2"]' in m.label


# ------------------------------------------------------- 对照组

import pytest

from aifix.adapters.base import Failure, FailureSet
from aifix.metamorphic import diverging_mutations

_TID = "tests/test_x.py::test_x"
_TEST = 'def test_x():\n    ids = ["q1", "q2", "q3"]\n    assert f(ids) == "ok"\n'


def _red() -> FailureSet:
    return FailureSet({_TID: Failure(test_id=_TID, classname="c", name="n",
                                     message="m", trace="t")})


def _green() -> FailureSet:
    return FailureSet({})


def _repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(_TEST, encoding="utf-8")
    (tmp_path / "p.py").write_text("PATCHED", encoding="utf-8")
    return tmp_path


async def test_a_patch_that_survives_the_mutation_says_nothing(tmp_path):
    wt = _repo(tmp_path)

    async def rerun(_ids):
        return _green()

    out = await diverging_mutations(wt, "tests/test_x.py",
                                    {"p.py": ("HEAD", "PATCHED")},
                                    _TID, rerun, max_mutations=5)
    assert out.diverged == []
    assert out.checked == 1
    assert out.discarded == 0


async def test_a_fitted_patch_is_reported(tmp_path):
    """变形后补丁挂了，而对照组仍然红 —— 变形有效，补丁没泛化。"""
    wt = _repo(tmp_path)

    async def rerun(_ids):
        return _red()          # 两问都红

    out = await diverging_mutations(wt, "tests/test_x.py",
                                    {"p.py": ("HEAD", "PATCHED")},
                                    _TID, rerun, max_mutations=5)
    assert len(out.diverged) == 1
    assert "第 2 行" in out.diverged[0].label
    assert out.discarded == 0


async def test_a_mutation_that_breaks_the_test_is_discarded(tmp_path):
    """对照组变绿 = 变形把测试本身搞坏了，这一个不作数，绝不报成补丁的问题。

    这是这一层不误伤的全部依据。少了它，任何一个动到期望值的扰动都会把一个
    完全正确的补丁判成「没泛化」。
    """
    wt = _repo(tmp_path)
    calls = []

    async def rerun(_ids):
        calls.append(1)
        return _red() if len(calls) == 1 else _green()

    out = await diverging_mutations(wt, "tests/test_x.py",
                                    {"p.py": ("HEAD", "PATCHED")},
                                    _TID, rerun, max_mutations=5)
    assert out.diverged == []
    assert out.discarded == 1


async def test_the_workspace_is_restored_byte_for_byte(tmp_path):
    wt = _repo(tmp_path)

    async def rerun(_ids):
        return _red()

    await diverging_mutations(wt, "tests/test_x.py",
                              {"p.py": ("HEAD", "PATCHED")},
                              _TID, rerun, max_mutations=5)
    assert (wt / "tests" / "test_x.py").read_text(encoding="utf-8") == _TEST
    assert (wt / "p.py").read_text(encoding="utf-8") == "PATCHED"


async def test_the_workspace_is_restored_even_when_rerun_blows_up(tmp_path):
    wt = _repo(tmp_path)

    async def rerun(_ids):
        raise RuntimeError("测试跑挂了")

    with pytest.raises(RuntimeError):
        await diverging_mutations(wt, "tests/test_x.py",
                                  {"p.py": ("HEAD", "PATCHED")},
                                  _TID, rerun, max_mutations=5)
    assert (wt / "tests" / "test_x.py").read_text(encoding="utf-8") == _TEST
    assert (wt / "p.py").read_text(encoding="utf-8") == "PATCHED"


async def test_the_control_run_really_puts_head_back(tmp_path):
    """对照组那一跑必须看到 HEAD 的产品代码，不是打了补丁的那份。"""
    wt = _repo(tmp_path)
    seen = []

    async def rerun(_ids):
        seen.append((wt / "p.py").read_text(encoding="utf-8"))
        return _red()

    await diverging_mutations(wt, "tests/test_x.py",
                              {"p.py": ("HEAD", "PATCHED")},
                              _TID, rerun, max_mutations=5)
    assert seen == ["PATCHED", "HEAD"]


async def test_nothing_to_mutate_is_not_a_clean_bill(tmp_path):
    """没有可扰动的字面量时 checked 为 0 —— 「没查」不能读成「很干净」。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert 1\n", encoding="utf-8")

    async def rerun(_ids):
        raise AssertionError("不该跑到")

    out = await diverging_mutations(tmp_path, "tests/test_x.py", {},
                                    _TID, rerun, max_mutations=5)
    assert out.checked == 0 and out.diverged == []


# ------------------------------------------------------- 接进 verify

async def test_verify_sends_back_a_patch_that_only_fits_one_ordering(
        tmp_path, monkeypatch):
    """PR #91 的真实形状：补丁把一串 id 拼成一整条去做全等匹配。

    原序下测试绿，换序就红；而换序后的测试在未打补丁的代码上仍然红（缺陷还在）
    —— 对照组成立，于是这是补丁的问题，退回重写。
    """
    from aifix.metamorphic import diverging_mutations

    wt = tmp_path
    (wt / "tests").mkdir()
    test_src = (
        'from app import drop\n\n\n'
        'def test_x():\n'
        '    steps = [{"result": "〔题目ID:q1,q2,q3〕"}]\n'
        '    drop(steps, ["q1", "q2", "q3"])\n'
        '    assert "〔题目ID:" not in steps[0]["result"]\n')
    (wt / "tests" / "test_x.py").write_text(test_src, encoding="utf-8")

    head = 'def drop(steps, purged):\n    return None\n'
    patched = ('def drop(steps, purged):\n'
               '    mark = "〔题目ID:" + ",".join(purged) + "〕"\n'
               '    for s in steps:\n'
               '        s["result"] = s["result"].replace(mark, "")\n')
    (wt / "app.py").write_text(patched, encoding="utf-8")

    async def rerun(_ids):
        """真跑：把当前的 app.py 与变形后的测试凑起来执行。"""
        ns: dict = {}
        exec((wt / "app.py").read_text(encoding="utf-8"), ns)
        src = (wt / "tests" / "test_x.py").read_text(encoding="utf-8")
        body = src.split("def test_x():", 1)[1]
        code = "def test_x():" + body
        exec(code, ns)
        try:
            ns["test_x"]()
        except AssertionError:
            return _red()
        return _green()

    out = await diverging_mutations(wt, "tests/test_x.py",
                                    {"app.py": (head, patched)},
                                    _TID, rerun, max_mutations=3)
    assert len(out.diverged) >= 1, "换序就挂的补丁必须被报出来"
    assert out.discarded == 0


async def test_a_generalising_patch_survives_the_same_check(tmp_path):
    """反向对照：按集合判断的正确补丁，换序照样绿，不能被误报。"""
    from aifix.metamorphic import diverging_mutations
    import re as _re

    wt = tmp_path
    (wt / "tests").mkdir()
    (wt / "tests" / "test_x.py").write_text(
        'def test_x():\n'
        '    steps = [{"result": "〔题目ID:q1,q2,q3〕"}]\n'
        '    drop(steps, ["q1", "q2", "q3"])\n'
        '    assert "〔题目ID:" not in steps[0]["result"]\n', encoding="utf-8")

    head = "def drop(steps, purged):\n    return None\n"
    patched = (
        "import re\n"
        "def drop(steps, purged):\n"
        "    dead = set(purged)\n"
        "    def keep(m):\n"
        "        left = [x for x in m.group(1).split(',') if x not in dead]\n"
        "        return '〔题目ID:' + ','.join(left) + '〕' if left else ''\n"
        "    for s in steps:\n"
        "        s['result'] = re.sub(r'〔题目ID:([^〕]+)〕', keep, s['result'])\n")
    (wt / "app.py").write_text(patched, encoding="utf-8")

    async def rerun(_ids):
        ns: dict = {}
        exec((wt / "app.py").read_text(encoding="utf-8"), ns)
        src = (wt / "tests" / "test_x.py").read_text(encoding="utf-8")
        exec(src, ns)
        try:
            ns["test_x"]()
        except AssertionError:
            return _red()
        return _green()

    out = await diverging_mutations(wt, "tests/test_x.py",
                                    {"app.py": (head, patched)},
                                    _TID, rerun, max_mutations=3)
    assert out.diverged == [], "正确的补丁不能被误报"
    assert out.checked >= 1, "而且必须真的查过"
