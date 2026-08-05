"""前后端同仓时，reproducer 自己选该写哪一套测试体系。

改造前 `detect_adapter` 只返回一个适配器（`AIFIX_ADAPTERS` 里的第一个），于是
ai-learning-helper 的每一条 issue 都拿到 pytest —— 包括报 `.tsx` 缺陷的那些。
用 pytest 写一条关于 `.tsx` 的测试，唯一的写法就是把它当文本读，那正是 #95
产出 grep 式假测试的原因。

见 docs/superpowers/specs/2026-08-05-reproduction-must-touch-project-design.md §5
"""
from aifix.agents.reproducer import Harness, build_prompt, owning_harness

_TITLE = "空对话引导少了一条建议"
_BODY = "EmptyHint 的建议列表里应该有「生成 5 道 AI 题」，现在没有。"

_PYTEST = Harness(name="pytest", test_dirs=["tests", "test"],
                  example_id="tests/test_calc.py::test_add")
_VITEST = Harness(name="vitest", test_dirs=["web/src"],
                  example_id="src/lib/calc.test.ts::calc > 两数相加")


# ------------------------------------------------------- 提示词


def test_single_harness_prompt_has_no_choice_language():
    """只有一套时不谈「选」—— 今天绝大多数仓库是这个形状，行为不该变。"""
    p = build_prompt(_TITLE, _BODY, [_PYTEST])
    assert "tests" in p
    assert "选" not in p


def test_single_harness_still_carries_example_id():
    p = build_prompt(_TITLE, _BODY, [_PYTEST])
    assert "tests/test_calc.py::test_add" in p


def test_multi_harness_prompt_lists_every_option():
    """两套的名字、目录、id 样例都要出现，否则模型只能猜。"""
    p = build_prompt(_TITLE, _BODY, [_PYTEST, _VITEST])
    for expect in ("pytest", "vitest", "tests", "web/src",
                   "tests/test_calc.py::test_add",
                   "src/lib/calc.test.ts::calc > 两数相加"):
        assert expect in p, expect


def test_multi_harness_prompt_says_how_to_choose():
    """判据必须写出来：按缺陷落在哪一侧的代码，不是按哪套测试多。

    不写的话模型会照着「测试目录列表最长的那个」或者干脆第一个选 —— 那就是
    改造前的行为，等于这一层白做。
    """
    p = build_prompt(_TITLE, _BODY, [_PYTEST, _VITEST])
    assert "缺陷" in p


def test_multi_harness_prompt_offers_the_honest_exit():
    """哪一套都写不出时，如实放弃比硬写一条假测试好 —— #95 的教训。"""
    p = build_prompt(_TITLE, _BODY, [_PYTEST, _VITEST])
    assert "can_reproduce" in p


# ------------------------------------------------------- 从 test_file 反推


class _FakeAdapter:
    """只实现 `is_test_path`，这是反推唯一要用的东西。"""

    def __init__(self, name, claims):
        self.name = name
        self._claims = claims

    def is_test_path(self, path: str) -> bool:
        return self._claims(path)


_A_PYTEST = _FakeAdapter("pytest", lambda p: p.startswith(("tests/", "test/")))
_A_VITEST = _FakeAdapter("vitest", lambda p: p.endswith((".test.ts", ".test.tsx")))


def test_owning_harness_picks_pytest_for_tests_dir():
    a = owning_harness("tests/test_x.py", [_A_PYTEST, _A_VITEST])
    assert a is _A_PYTEST


def test_owning_harness_picks_vitest_for_test_suffix():
    a = owning_harness("web/src/components/EmptyHint.test.tsx",
                       [_A_PYTEST, _A_VITEST])
    assert a is _A_VITEST


def test_owning_harness_breaks_ties_by_given_order():
    """`tests/a.test.ts` 两套都认领 —— 取给定顺序里的第一个。

    顺序来自 `AIFIX_ADAPTERS`，那是人对这个仓库的判断。平局时听人的，
    而不是听一个「哪个更具体」的启发式。
    """
    assert owning_harness("tests/a.test.ts", [_A_PYTEST, _A_VITEST]) is _A_PYTEST
    assert owning_harness("tests/a.test.ts", [_A_VITEST, _A_PYTEST]) is _A_VITEST


def test_owning_harness_returns_none_when_unclaimed():
    """没人认领是**有意义的结论**：这条路径不是任何一套体系的测试文件。"""
    assert owning_harness("app/main.py", [_A_PYTEST, _A_VITEST]) is None
