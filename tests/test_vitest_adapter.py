"""VitestAdapter。

下面那份 `_REAL_XML` 是**真跑抄下来的**（vitest 2.1.9 / node 24，2026-08-03），
只把 timestamp / hostname / time 三个每次都变的属性抹平了。手写的 JUnit 只能证明
我们理解得自洽，证明不了 vitest 真的这么写 —— 而这个适配器每一条判断的依据都是
「vitest 实际写成什么样」。

重新生成的办法：在一个装了 vitest 的工程里造出 `src/lib/calc.ts`（inner 抛异常、
outer 调 inner、add 故意算错）和两个测试文件（一个正常、一个 import 不存在的模块），
然后 `node_modules/.bin/vitest run --reporter=junit --outputFile=x.xml`。

真跑那条挂了 skip，判据与 Maven 那几条同款：离线装得上就跑，装不上就跳。
纯逻辑的那些不挂 —— 本机没有 node 工具链时它们照样有价值。
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from aifix.adapters.junit import parse_junit
from aifix.adapters.vitest_adapter import VitestAdapter, find_pkg_dir

_REAL_XML = '''<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="5" failures="3" errors="0" time="0">
    <testsuite name="src/broken.test.ts" timestamp="T" hostname="H" tests="1" failures="1" errors="0" skipped="0" time="0">
        <testcase classname="src/broken.test.ts" name="src/broken.test.ts" time="0">
            <failure message="Failed to load url ./does_not_exist" type="Error">
Error: Failed to load url ./does_not_exist
 ❯ loadAndTransform ../../elsewhere/node_modules/vite/dist/node/chunks/dep-BK3b2jBa.js:51969:17
            </failure>
        </testcase>
    </testsuite>
    <testsuite name="src/lib/calc.test.ts" timestamp="T" hostname="H" tests="4" failures="2" errors="0" skipped="0" time="0">
        <testcase classname="src/lib/calc.test.ts" name="calc &gt; 断言失败" time="0">
            <failure message="expected -1 to be 5 // Object.is equality" type="AssertionError">
AssertionError: expected -1 to be 5 // Object.is equality
 ❯ src/lib/calc.test.ts:5:40
            </failure>
        </testcase>
        <testcase classname="src/lib/calc.test.ts" name="calc &gt; 产品代码抛异常" time="0">
            <failure message="负数没准备好" type="Error">
Error: 负数没准备好
 ❯ inner src/lib/calc.ts:2:20
 ❯ Module.outer src/lib/calc.ts:5:51
 ❯ src/lib/calc.test.ts:6:25
            </failure>
        </testcase>
        <testcase classname="src/lib/calc.test.ts" name="calc &gt; 通过的" time="0">
        </testcase>
        <testcase classname="src/lib/calc.test.ts" name="calc &gt; 名字带 (括号) 和 [方括号]" time="0">
        </testcase>
    </testsuites_PLACEHOLDER>
'''.replace("</testsuites_PLACEHOLDER>", "</testsuite>\n</testsuites>")


def _repo(tmp_path: Path, *, pkg_dir: str = "") -> Path:
    """造出栈帧指向的那几个**真实文件** —— locate_source 会做存在性检查。

    写死一个不存在的路径，这条测试就退化成在验证一个字符串函数对虚构输入的行为。
    """
    base = tmp_path / pkg_dir if pkg_dir else tmp_path
    (base / "src" / "lib").mkdir(parents=True)
    (base / "src" / "lib" / "calc.ts").write_text("x\n" * 10, encoding="utf-8")
    (base / "src" / "lib" / "calc.test.ts").write_text("x\n" * 10, encoding="utf-8")
    (base / "src" / "broken.test.ts").write_text("x\n", encoding="utf-8")
    return tmp_path


def _failures(tmp_path: Path, adapter: VitestAdapter):
    p = tmp_path / "r.xml"
    p.write_text(_REAL_XML, encoding="utf-8")
    return parse_junit([p], adapter.make_test_id)


# ------------------------------------------------------------------ detect

def test_detect_needs_vitest_in_the_dependencies(tmp_path):
    """光有 package.json 不算数。

    带一点前端工具链（eslint、prettier、一个构建脚本）的 Python / Java 工程
    也有 package.json，而认错的后果不是报错 —— baseline 会去跑一个不存在的
    vitest，或者跑出 0 个用例，报告写「这个仓库没问题」。
    """
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"eslint": "^9"}}), encoding="utf-8")
    assert VitestAdapter.detect(tmp_path) is False

    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^2"}}), encoding="utf-8")
    assert VitestAdapter.detect(tmp_path) is True


def test_detect_is_false_without_package_json(tmp_path):
    assert VitestAdapter.detect(tmp_path) is False


def test_a_broken_package_json_is_not_an_exception(tmp_path):
    """读不动就当没有。这里抛异常的话，异常会从 preflight 或 baseline 里钻出来，
    而那只说明这个仓库的 package.json 坏了，不说明它是个 vitest 工程。"""
    (tmp_path / "package.json").write_text("{ 这不是 JSON", encoding="utf-8")
    assert VitestAdapter.detect(tmp_path) is False


# -------------------------------------------------------------- is_test_path

def test_test_files_are_recognised_by_suffix_not_by_directory():
    """**这个适配器存在的全部理由。**

    两个文件同目录，一个是测试一个不是 —— 任何基于目录的判据在这里都只能
    二选一地错：把整个 src 判成测试（fixer 什么都改不了），或者一个都不判
    （守卫静默放行，fixer 可以直接改掉自己的判卷标准）。
    """
    a = VitestAdapter()
    assert a.is_test_path("src/components/ChatView.test.tsx") is True
    assert a.is_test_path("src/components/ChatView.tsx") is False
    assert a.is_test_path("src/api/sse.spec.ts") is True
    assert a.is_test_path("src/api/sse.ts") is False


def test_the_write_guard_actually_refuses_a_colocated_test_file(tmp_path):
    """端到端到守卫那一层：谓词接上去之后，同目录的测试文件真的改不动。

    只断言 `is_test_path` 返回 True 是不够的 —— 它可能压根没被接到守卫上，
    而那种漏接不报错。
    """
    from harness.tools.base import ToolError

    from aifix.tools.guard import guard_write

    class _SB:
        workspace = str(tmp_path)

    a = VitestAdapter()
    with pytest.raises(ToolError, match="拒绝修改测试文件"):
        guard_write(_SB(), a.is_test_path, "src/components/ChatView.test.tsx")
    # 反向：同目录的产品文件必须放行，否则 fixer 什么都改不了
    guard_write(_SB(), a.is_test_path, "src/components/ChatView.tsx")


# -------------------------------------------------------------- make_test_id

def test_a_file_level_failure_becomes_a_bare_path(tmp_path):
    """整个文件没能加载时 vitest 发的是 `classname == name == 文件路径`。

    拼成 `文件::文件` 会得到一个跑不起来的 id，而无效 id 的代价不是报错：
    它进了命令行、一个用例都不跑、写出一份空报告，看起来像「复跑全通过」。
    """
    a = VitestAdapter()
    fs = _failures(tmp_path, a)
    assert "src/broken.test.ts" in fs.ids
    assert a.is_file_level_id("src/broken.test.ts") is True


def test_a_normal_case_keeps_the_describe_chain(tmp_path):
    a = VitestAdapter()
    fs = _failures(tmp_path, a)
    assert "src/lib/calc.test.ts::calc > 断言失败" in fs.ids
    assert a.is_file_level_id("src/lib/calc.test.ts::calc > 断言失败") is False


def test_cases_under_compares_the_separator_not_a_bare_prefix():
    a = VitestAdapter()
    ids = frozenset({"src/a.test.ts::x", "src/ab.test.ts::y"})
    assert a.cases_under("src/a.test.ts", ids) == {"src/a.test.ts::x"}


# ------------------------------------------------------------ scoped 选择器

def test_the_report_separator_is_not_the_selector_separator():
    """报告写 ` > `，而 `-t` 匹配的是**空格**连接的那份。

    已实测：`-t "外层 > 内层 > 普通用例"` 跑了 0 条，`-t "外层 内层 普通用例"`
    才命中。报告里的 id 直接拿去当选择器，一条用例都匹配不到 —— 而 vitest
    不会因此报错，它只是安静地跑了个空。
    """
    cmd = VitestAdapter().scoped_test_command(["src/a.test.ts::外层 > 内层 > 用例"])
    assert "-t" in cmd
    pat = cmd[cmd.index("-t") + 1]
    assert " > " not in pat
    assert "外层 内层 用例" in pat


def test_the_selector_is_anchored():
    """不加 `^...$` 会误伤前缀。

    已实测：`-t "外层 前缀"` 同时跑了 `外层 > 前缀` 和 `外层 > 前缀加长` ——
    复跑多跑用例会污染 flaky 确认与红检的判定集合。
    """
    cmd = VitestAdapter().scoped_test_command(["src/a.test.ts::外层 > 前缀"])
    pat = cmd[cmd.index("-t") + 1]
    assert pat.startswith("^") and pat.endswith("$")


def test_regex_metacharacters_in_a_test_name_are_escaped():
    """`-t` 收的是**正则**。已实测：名字里带 `(括号)` 时不转义就匹配不到 ——
    括号被当成了捕获组。"""
    cmd = VitestAdapter().scoped_test_command(
        ["src/a.test.ts::calc > 名字带 (括号) 和 [方括号]"])
    pat = cmd[cmd.index("-t") + 1]
    assert r"\(" in pat and r"\[" in pat
    # 空格**不能**转义：JS 正则在 u 标志下把 `\ ` 当作非法的身份转义
    assert "\\ " not in pat


def test_several_cases_share_one_pattern():
    """`-t` 只能给一次，多个用例合成正则的选择分支。"""
    cmd = VitestAdapter().scoped_test_command(
        ["src/a.test.ts::x", "src/a.test.ts::y"])
    pat = cmd[cmd.index("-t") + 1]
    assert pat == "^x$|^y$"
    assert cmd.count("src/a.test.ts") == 1, "同一个文件不该重复出现"


def test_a_file_level_id_suppresses_the_name_filter():
    """文件级 id 名下没有用例名可写进选择分支，而发一个匹配不到它的 `-t`
    会让它被跳过 —— 于是「这个文件整体加载失败」在复跑结果里消失。

    多跑几条用例是可承受的，把一整类失败跑丢不是。
    """
    cmd = VitestAdapter().scoped_test_command(
        ["src/broken.test.ts", "src/a.test.ts::x"])
    assert "-t" not in cmd
    assert "src/broken.test.ts" in cmd and "src/a.test.ts" in cmd


# ------------------------------------------------------------- locate_source

def test_frames_are_deepest_first(tmp_path):
    """vitest 的栈由深入浅打印（栈顶就是抛出点），与 Java 一致、与 Python 相反。

    弄反的后果是「最可疑的位置」指向测试文件里那行调用，而不是真正抛异常的
    产品代码。
    """
    a = VitestAdapter()
    _repo(tmp_path)
    fs = _failures(tmp_path, a)
    f = fs.failures["src/lib/calc.test.ts::calc > 产品代码抛异常"]
    got = [(c.path, c.line) for c in a.locate_source(f, tmp_path)]
    assert got == [("src/lib/calc.ts", 2), ("src/lib/calc.ts", 5),
                   ("src/lib/calc.test.ts", 6)], got
    assert a.locate_source(f, tmp_path)[0].frame == "inner"


def test_a_frame_without_a_function_name_still_parses(tmp_path):
    """入口帧长成 ` ❯ src/a.test.ts:4:19`，没有函数名那一段。"""
    a = VitestAdapter()
    _repo(tmp_path)
    fs = _failures(tmp_path, a)
    f = fs.failures["src/lib/calc.test.ts::calc > 断言失败"]
    got = [(c.path, c.line, c.frame) for c in a.locate_source(f, tmp_path)]
    assert got == [("src/lib/calc.test.ts", 5, "")], got


def test_node_modules_frames_are_dropped(tmp_path):
    """加载失败的栈顶是 vite 自己的帧（实测：`loadAndTransform ../../..`）。

    `SourceCandidate.path` 会原样进模型的提示词，把框架内部文件递给它只会让
    它去读一个与缺陷无关的几万行文件。
    """
    a = VitestAdapter()
    _repo(tmp_path)
    fs = _failures(tmp_path, a)
    f = fs.failures["src/broken.test.ts"]
    assert a.locate_source(f, tmp_path) == []


# --------------------------------------------------------------- pkg_dir

def test_ids_are_repo_relative_while_selectors_are_root_relative(tmp_path):
    """前端在子目录时，两个坐标系必须各自说各自的话。

    报告里的 classname 相对 `--root`（实测：`--root web` 跑出来是
    `src/a.test.ts`，不是 `web/src/a.test.ts`），而这个适配器对外一律说**仓库
    相对**路径 —— 那是核心循环、`is_test_path` 与交付记账共同的坐标系。
    翻回去是 `scoped_test_command` 的事。
    """
    a = VitestAdapter(pkg_dir="web")
    _repo(tmp_path, pkg_dir="web")
    fs = _failures(tmp_path, a)

    tid = "web/src/lib/calc.test.ts::calc > 断言失败"
    assert tid in fs.ids, sorted(fs.ids)

    # 选择器要把前缀脱掉，否则 vitest 在 web/ 下找 web/src/... 找不到
    cmd = a.scoped_test_command([tid])
    assert "src/lib/calc.test.ts" in cmd
    assert "web/src/lib/calc.test.ts" not in cmd
    assert cmd[0] == "web/node_modules/.bin/vitest"
    assert "--root" in cmd and cmd[cmd.index("--root") + 1] == "web"

    # 栈帧同样要加回前缀，否则存在性检查落空、一个候选都定位不到
    f = fs.failures["web/src/lib/calc.test.ts::calc > 产品代码抛异常"]
    assert [c.path for c in a.locate_source(f, tmp_path)][0] == \
        "web/src/lib/calc.ts"


def test_the_report_lands_under_the_root(tmp_path):
    """`--outputFile` 相对 `--root` 解析，不是相对 cwd —— 已实测：从父目录带
    `--root web` 跑，报告落在 `web/` 下。找错地方 = 报告「不存在」= 整个 run
    被判成没跑成。"""
    a = VitestAdapter(pkg_dir="web")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / a.REPORT_NAME).write_text("<x/>", encoding="utf-8")
    assert a.report_paths(tmp_path) == [tmp_path / "web" / a.REPORT_NAME]


# ------------------------------------------------------------------ 命令形状

def test_the_command_never_goes_through_npx_or_the_project_script():
    """项目的 `test` 脚本内容是它自己定的。写成 `vitest`（不带 `run`）就是
    **watch 模式** —— 进程永远不退出，整个 run 挂到墙钟闸响。而 `vitest` 与
    `vitest run` 只差一个词。
    """
    cmd = VitestAdapter().full_test_command()
    assert cmd[0] == "node_modules/.bin/vitest"
    assert cmd[1] == "run"
    assert "npx" not in cmd and "npm" not in cmd


def test_test_selectors_drop_non_test_files():
    """测试目录下的夹具（快照、fixture JSON）跟着测试一起进 test_files，
    但放到 vitest 命令行上会变成「匹配不到任何测试文件」而一个用例都不跑。"""
    a = VitestAdapter()
    assert a.test_selectors(
        ["src/a.test.ts", "src/__snapshots__/a.snap", "src/a.ts"]) == \
        ["src/a.test.ts"]


# ------------------------------------------------------------------- 真跑

_PROBE_PKG = {"name": "aifix-vitest-probe", "private": True, "type": "module"}


@functools.lru_cache(maxsize=1)
def _vitest_offline_reason() -> str | None:
    """离线装得上 vitest 就返回 None，否则返回跳过的理由。整个会话只探一次。

    用 `--offline` 而不是 `--prefer-offline`：与 Maven 那几条的 `mvn -o` 同一条
    理由 —— 让测试套件依赖网络会把一类随机失败引进 CI，而那类失败与代码无关。
    """
    if shutil.which("npm") is None:
        return "本机没有 npm"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps(_PROBE_PKG),
                                           encoding="utf-8")
        try:
            res = subprocess.run(
                ["npm", "install", "--offline", "--no-audit", "--no-fund",
                 "--silent", "vitest"],
                cwd=root, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            return "npm install 跑不起来"
    if res.returncode != 0:
        return "本机 npm 缓存里没有 vitest，`npm install --offline` 装不上"
    return None


def _vitest_skip():
    reason = _vitest_offline_reason()
    return pytest.mark.skipif(reason is not None,
                              reason=f"跳过 vitest 真跑：{reason}")


@_vitest_skip()
def test_the_built_selector_really_selects_exactly_that_one_case(tmp_path):
    """**这条是整个适配器里最容易错、也最该真跑的一件事。**

    分隔符翻译、锚点、元字符转义三样合起来才能精确命中一条用例，而三样错任何
    一样的表现都不是报错：vitest 安静地跑 0 条、或者多跑几条。断言的是
    「跑出结果的恰好是我们点名的那一条」，其余全被 `-t` 标成 skipped。
    """
    subprocess.run(["npm", "install", "--offline", "--no-audit", "--no-fund",
                    "--silent", "vitest"], cwd=tmp_path, check=True,
                   capture_output=True, timeout=180)
    (tmp_path / "package.json").write_text(json.dumps(_PROBE_PKG),
                                           encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.test.ts").write_text(
        "import { describe, it, expect } from 'vitest'\n"
        "describe('外层', () => {\n"
        "  it('前缀', () => { expect(1).toBe(1) })\n"
        "  it('前缀加长', () => { expect(1).toBe(1) })\n"
        "  it('带 (括号) 的', () => { expect(1).toBe(1) })\n"
        "})\n", encoding="utf-8")

    a = VitestAdapter()
    for target, want in (("外层 > 前缀", "外层 > 前缀"),
                         ("外层 > 带 (括号) 的", "外层 > 带 (括号) 的")):
        cmd = a.scoped_test_command([f"src/a.test.ts::{target}"])
        subprocess.run(cmd, cwd=tmp_path, capture_output=True, timeout=180)
        fs = parse_junit(a.report_paths(tmp_path, scoped=True), a.make_test_id)
        assert fs.ran == {f"src/a.test.ts::{want}"}, sorted(fs.ran)


# --------------------------------------------------- package.json 在哪

def _pkg(d: Path, *, vitest: bool = True) -> None:
    d.mkdir(parents=True, exist_ok=True)
    deps = {"vitest": "^2"} if vitest else {"jest": "^29"}
    (d / "package.json").write_text(json.dumps({"devDependencies": deps}),
                                    encoding="utf-8")


def test_pkg_dir_is_found_at_the_root(tmp_path):
    _pkg(tmp_path)
    assert find_pkg_dir(tmp_path) == ""


def test_pkg_dir_is_found_one_level_down(tmp_path):
    """**前后端同仓的真实形状。** ai-learning-helper 的前端就在 `web/` 下。

    只看根目录的话，`AIFIX_ADAPTERS=pytest,vitest` 配了也白配 —— 探测说这里
    没有前端，而那与真的没有前端完全无法区分。
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n",
                                             encoding="utf-8")
    _pkg(tmp_path / "web")
    assert find_pkg_dir(tmp_path) == "web"
    assert VitestAdapter.detect(tmp_path) is True


def test_node_modules_is_never_walked_into(tmp_path):
    """`node_modules` 里每个包都有自己的 package.json，其中不少也依赖 vitest ——
    走进去必然认错，而认错的表现是 `--root node_modules` 跑了个空。"""
    _pkg(tmp_path / "node_modules" / "some-pkg")
    assert find_pkg_dir(tmp_path) is None


def test_a_frontend_without_vitest_is_not_claimed(tmp_path):
    """依赖里是 jest 不是 vitest —— 不认领。认错的后果是 baseline 去跑一个
    不存在的二进制，报告写「测试进程没能正常跑完」。"""
    _pkg(tmp_path / "web", vitest=False)
    assert find_pkg_dir(tmp_path) is None
    assert VitestAdapter.detect(tmp_path) is False


def test_the_subdir_choice_is_reproducible(tmp_path):
    """两个子目录都有前端时按名字排序取第一个。

    不定死顺序的话，同一个仓库在两台机器上可能选中不同的前端，而两边都不报错。
    """
    _pkg(tmp_path / "zui")
    _pkg(tmp_path / "app")
    assert find_pkg_dir(tmp_path) == "app"


# ------------------------------------------------------------- prepare

def test_prepare_borrows_node_modules_into_the_worktree(tmp_path):
    """worktree 只含被 git 跟踪的文件，而 `node_modules` 当然不被跟踪 ——
    不借的话 `node_modules/.bin/vitest` 根本不存在。"""
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    (repo / "node_modules" / "react").mkdir(parents=True)
    (repo / "node_modules" / ".bin").mkdir()
    tree.mkdir()

    VitestAdapter(repo=repo).prepare(tree)
    assert (tree / "node_modules").is_dir()
    assert not (tree / "node_modules").is_symlink(), \
        "必须是真目录 —— 整个软链过去的话缓存会写进源仓库"
    assert (tree / "node_modules" / "react").is_symlink()
    assert (tree / "node_modules" / ".bin").is_symlink()


def test_prepare_is_idempotent(tmp_path):
    """`run_full_suite` 与 `run_scoped` 各调一次，而一次 run 里它们要跑好几轮。"""
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    (repo / "node_modules" / "react").mkdir(parents=True)
    tree.mkdir()
    a = VitestAdapter(repo=repo)
    a.prepare(tree)
    a.prepare(tree)          # 第二次不该抛
    assert (tree / "node_modules" / "react").is_symlink()


def test_prepare_does_nothing_without_a_source(tmp_path):
    """源仓库里也没有 node_modules 时什么都不做、不报错 —— 那时正确的动作是
    让 vitest 自己以「找不到命令」失败，那句报错比我们编一句准确。"""
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    repo.mkdir()
    tree.mkdir()
    VitestAdapter(repo=repo).prepare(tree)
    assert not (tree / "node_modules").exists()


def test_prepare_handles_the_subdir_layout(tmp_path):
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    (repo / "web" / "node_modules" / "react").mkdir(parents=True)
    _pkg(repo / "web")
    (tree / "web").mkdir(parents=True)

    a = VitestAdapter(repo=repo)
    assert a.pkg_dir == "web", "构造时应当自己探到"
    a.prepare(tree)
    assert (tree / "web" / "node_modules" / "react").is_symlink()


@_vitest_skip()
def test_the_dependency_cache_lands_in_the_worktree_not_the_source(tmp_path):
    """**这条是逐个子项软链存在的全部理由。**

    实测：vitest 跑完会在 `node_modules/.vite` 下写依赖预构建缓存。整个
    `node_modules` 软链过去的话，那些写**落在源仓库里** —— 而这个项目的地基是
    「agent 的一切改动都发生在 worktree，主工作区绝不被触碰」。
    """
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    repo.mkdir()
    subprocess.run(["npm", "install", "--offline", "--no-audit", "--no-fund",
                    "--silent", "vitest"], cwd=repo, check=True,
                   capture_output=True, timeout=180)
    (repo / "package.json").write_text(
        json.dumps({"private": True, "type": "module",
                    "devDependencies": {"vitest": "^2"}}), encoding="utf-8")
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('绿的', () => { expect(1).toBe(1) })\n", encoding="utf-8")

    a = VitestAdapter(repo=repo)
    a.prepare(tree)
    subprocess.run(a.full_test_command(), cwd=tree, capture_output=True,
                   timeout=180)

    assert (tree / a.REPORT_NAME).is_file(), "报告要写出来，说明真跑了"
    assert (tree / "node_modules" / ".vite").exists(), "缓存该落在 worktree"
    assert not (repo / "node_modules" / ".vite").exists(), \
        "源仓库被写进去了 —— worktree 隔离被击穿"
