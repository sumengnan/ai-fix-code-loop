"""复现测试必须真的接触本项目的代码。

起因是 ai-learning-helper#95：模型写出一条对 `EmptyHint.tsx` 做字符串 grep 的
pytest 测试，红检放行、报告写「修复 1/1」，而把补丁整个撤销、只留一句「还没加」
的注释，它照样绿。

见 docs/superpowers/specs/2026-08-05-reproduction-must-touch-project-design.md
"""
from pathlib import Path

from aifix.agents.reproducer import project_module_roots, touches_project

# ai-learning-helper#95 产出的那条测试，**逐字**。
#
# 它是这道闸存在的理由，所以钉成回归样本：将来任何改动让它重新通过，
# 这个用例就该红。
_GREP_STYLE = '''\
from pathlib import Path

import pytest

_EMPTY_HINT = Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "EmptyHint.tsx"


def test_suggestions_contains_generate_5_ai_questions():
    """SUGGESTIONS 数组必须包含「生成5道ai题」这条默认对话。"""
    text = _EMPTY_HINT.read_text(encoding="utf-8")
    assert "生成5道ai题" in text, (
        "SUGGESTIONS 中缺少「生成5道ai题」这条默认对话，请添加到 EmptyHint.tsx 的 SUGGESTIONS 数组"
    )
'''


def _touch(repo: Path, rel: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


# ----------------------------------------------------------- 判据本身


def test_grep_style_reproduction_is_flagged():
    """#95 那条：import 的只有 pathlib 与 pytest，一个都不沾本项目。"""
    assert touches_project("tests/test_x.py", _GREP_STYLE, {"app", "web"}) is False


def test_importing_project_module_passes():
    code = "from app.exporter import export_csv\n\n\ndef test_x():\n    assert export_csv()\n"
    assert touches_project("tests/test_x.py", code, {"app"}) is True


def test_plain_import_of_project_package_passes():
    assert touches_project("tests/t.py", "import app\n", {"app"}) is True


def test_dotted_import_counts_by_root():
    """`import app.sub.mod` 的根名是 app —— 按根名比，不是整条点分路径。"""
    assert touches_project("tests/t.py", "import app.sub.mod\n", {"app"}) is True


def test_relative_import_counts_as_touching():
    """相对 import 没有根名可比，但它按定义就是在引用本包内的东西。

    roots 给非空的：空集合走的是「没有参照系、不表态」那条早退（见
    test_no_known_roots_means_no_verdict），两件事不该在一个用例里混着断言。
    """
    assert touches_project("tests/t.py", "from . import helper\n", {"app"}) is True


def test_import_inside_function_counts():
    """判据走 ast.walk，函数体里的延迟 import 同样算数。"""
    code = "def test_x():\n    from app.mod import f\n    assert f()\n"
    assert touches_project("tests/t.py", code, {"app"}) is True


def test_no_imports_at_all_is_flagged():
    assert touches_project("tests/t.py", "def test_x():\n    assert 1 + 1 == 2\n", {"app"}) is False


# ----------------------------------------------------------- 失效方向


def test_repo_dir_shadowing_stdlib_underreports():
    """仓库里恰好有个顶层目录叫 json 时，`import json` 被当成本项目模块。

    这是**漏报**，是设计不是 bug：判据只会放过一条坏测试（与今天的行为一样），
    绝不会误杀一条合法测试。方向见规格 §3.1。
    """
    assert touches_project("tests/t.py", "import json\n", {"json"}) is True


def test_syntax_error_is_not_a_verdict():
    """语法错是收集阶段那道闸的活，这里放行 —— 报「没接触本项目」是句假话。"""
    assert touches_project("tests/t.py", "def test_x(:\n", {"app"}) is None


def test_non_python_test_is_not_checked():
    """判据是 ast，Python-only（规格 §6）。别的语言记成「没查」而不是「没问题」。"""
    code = 'import { SUGGESTIONS } from "./EmptyHint";\n'
    assert touches_project("web/src/x.test.ts", code, {"web"}) is None


# ----------------------------------------------------------- 仓库顶层名


def test_roots_include_package_with_init(tmp_path):
    _touch(tmp_path, "app/__init__.py")
    assert "app" in project_module_roots(tmp_path)


def test_roots_include_namespace_package_without_init(tmp_path):
    """没有 __init__.py 的顶层目录同样算 —— PEP 420 命名空间包是真实形态。

    ai-learning-helper 的 agents/、mcp/、src/ 都没有 __init__.py，要求它
    会让判据在那个仓库上恒不响。
    """
    (tmp_path / "agents").mkdir()
    assert "agents" in project_module_roots(tmp_path)


def test_roots_include_top_level_module_file(tmp_path):
    _touch(tmp_path, "settings.py")
    assert "settings" in project_module_roots(tmp_path)


def test_roots_include_src_layout(tmp_path):
    """src/ 布局再看一层：src/aifix 的可 import 名是 aifix。"""
    _touch(tmp_path, "src/aifix/__init__.py")
    roots = project_module_roots(tmp_path)
    assert "aifix" in roots


def test_roots_exclude_test_dirs(tmp_path):
    """测试目录不算本项目模块 —— 一条只 import 别的测试的复现测试没有接触产品代码。"""
    (tmp_path / "tests").mkdir()
    assert "tests" not in project_module_roots(tmp_path)


def test_roots_exclude_dotted_and_hidden(tmp_path):
    """`.venv` / `.git` 不是可 import 的名字，混进来只会制造漏报。"""
    (tmp_path / ".venv").mkdir()
    (tmp_path / "node_modules").mkdir()
    roots = project_module_roots(tmp_path)
    assert ".venv" not in roots
    assert "node_modules" not in roots


def test_no_known_roots_means_no_verdict():
    """一个顶层模块都认不出来时不表态 —— 那不是查出了问题，是没有参照系。

    roots 为空时任何 import 都匹配不上，判 False 会让这道闸在最小仓库上恒亮，
    而恒亮的闸等于没有闸，还白花一轮重写的钱。
    """
    assert touches_project("tests/t.py", "from app.x import y\n", set()) is None
    assert touches_project("tests/t.py", "import pytest\n", set()) is None
