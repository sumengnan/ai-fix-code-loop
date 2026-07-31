from harness.sandbox.local import LocalSandbox

from aifix.adapters.base import Failure
from aifix.adapters.pytest_adapter import PytestAdapter
from aifix.agents.detector import Diagnosis
from aifix.agents.fixer import build_initial_messages, build_registry

_FAILURE = Failure(
    test_id="tests/test_calc.py::test_add", classname="tests.test_calc",
    name="test_add", message="assert -1 == 5", trace="E assert -1 == 5")
_DIAG = Diagnosis(suspect_file="calc.py", suspect_lines=(1, 2),
                  root_cause="减号应为加号", fix_strategy="改回 a + b",
                  confidence="high")


async def test_registry_exposes_exactly_five_tools(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids={_FAILURE.test_id})
        assert {t.name for t in reg.tools()} == {
            "read_file", "list_files", "grep", "apply_patch", "run_tests"}
    finally:
        await sb.close()


async def test_registry_has_no_shell(buggy_repo):
    """关键约束：能力面是白名单，绝不注册 shell。"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids=set())
        assert reg.get("run_shell") is None
        assert reg.get("run_python") is None
    finally:
        await sb.close()


def test_initial_messages_include_diagnosis():
    msgs = build_initial_messages(_FAILURE, _DIAG)
    blob = "\n".join(str(m.content) for m in msgs)
    assert "calc.py" in blob
    assert "减号应为加号" in blob
    assert _FAILURE.test_id in blob


def test_initial_messages_degrade_without_diagnosis():
    msgs = build_initial_messages(_FAILURE, None)
    blob = "\n".join(str(m.content) for m in msgs)
    assert "E assert -1 == 5" in blob
    assert msgs, "降级时仍须给出可用的初始消息"


async def test_registry_wires_touched_collector(buggy_repo):
    """收集器要真的接到 apply_patch 上，否则交付阶段拿不到路径。"""
    from harness.tools.base import ToolExecutor
    from harness.types import ToolCall

    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
+    return a + b
"""
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        touched: set[str] = set()
        reg = build_registry(sb, PytestAdapter(), known_ids=set(),
                             touched=touched)
        ex = ToolExecutor(reg, max_chars=8000)
        r = await ex.execute(ToolCall(id="1", name="apply_patch",
                                      arguments={"diff": patch}))
        assert not r.is_error, r.content
        assert touched == {"calc.py"}
    finally:
        await sb.close()


async def test_registry_touched_is_optional(buggy_repo):
    sb = LocalSandbox(workspace=str(buggy_repo))
    await sb.start()
    try:
        reg = build_registry(sb, PytestAdapter(), known_ids=set())
        assert reg.get("apply_patch") is not None
    finally:
        await sb.close()


# ---------- 无锚点诊断：别让模型靠猜路径开局 ----------
#
# 实跑打出来的：纯断言失败的 traceback 里只有测试文件那一帧，detector 只能按
# 包名猜源码路径，猜出来的是 `cart.py`（真身在 `src/shopcart/cart.py`）。这份
# 猜测被原样写进 Fixer 的开场白，于是它照着去 read_file，报「文件不存在」，
# 然后开始一层层 list_files 试探 —— 一次 run 里 10 次 list_files、16 次
# read_file，其中三次是对着不存在的路径。
#
# 系统本来就知道这份诊断没有锚点（facts.jsonl 里有 suspect_unanchored: true，
# 进度上也印着「无源码锚点」），只是没人拿它做任何事。

async def _hint(repo, diagnosis):
    from aifix.agents.fixer import locate_hint
    sb = LocalSandbox(workspace=str(repo))
    await sb.start()
    try:
        return await locate_hint(sb, PytestAdapter(), diagnosis)
    finally:
        await sb.close()


async def test_no_hint_when_the_suspect_really_exists(buggy_repo):
    """诊断指得准就闭嘴 —— 多一句都是在挤占上下文。"""
    assert await _hint(buggy_repo, _DIAG) is None


async def test_a_missing_suspect_is_resolved_by_name(buggy_repo):
    """猜错路径但文件名对得上（常态）：直接把真路径给它。

    `cart.py` → `src/shopcart/cart.py` 这一步 git 一次 ls-files 就能答，
    让模型用四五次工具调用去试探是纯粹的浪费。
    """
    (buggy_repo / "pkg").mkdir()
    (buggy_repo / "pkg" / "cart.py").write_text("x = 1\n", encoding="utf-8")
    _git = __import__("subprocess").run
    _git(["git", "add", "-A"], cwd=buggy_repo, check=True,
         capture_output=True)

    diag = Diagnosis(suspect_file="src/cart.py", suspect_lines=None,
                     root_cause="x", fix_strategy="y", confidence="low")
    hint = await _hint(buggy_repo, diag)
    assert hint and "src/cart.py" in hint, "要说清哪个路径是不存在的"
    assert "pkg/cart.py" in hint, "同名文件的真实路径要给出来"


async def test_an_unresolvable_suspect_gets_the_source_listing(buggy_repo):
    """连同名文件都没有：给出源码文件清单，仍然好过让它猜。"""
    diag = Diagnosis(suspect_file="totally/unknown.py", suspect_lines=None,
                     root_cause="x", fix_strategy="y", confidence="low")
    hint = await _hint(buggy_repo, diag)
    assert hint and "calc.py" in hint
    assert "tests/test_calc.py" not in hint, "测试文件改不得，列出来只会误导"


async def test_the_listing_also_shows_up_without_any_diagnosis(buggy_repo):
    """诊断解析失败的降级路径同理 —— 那时模型手里只有一段 traceback。"""
    hint = await _hint(buggy_repo, None)
    assert hint and "calc.py" in hint


async def test_a_big_repo_gets_advice_instead_of_a_wall_of_paths(buggy_repo):
    """大仓库不列清单。

    几千行路径塞进开场白，既烧钱又淹掉真正有用的那几句；那种规模下正确的
    动作是 grep 符号名，不是读目录。
    """
    (buggy_repo / "many").mkdir()
    for i in range(80):
        (buggy_repo / "many" / f"m{i}.py").write_text("x\n", encoding="utf-8")
    __import__("subprocess").run(["git", "add", "-A"], cwd=buggy_repo,
                                 check=True, capture_output=True)
    diag = Diagnosis(suspect_file="totally/unknown.py", suspect_lines=None,
                     root_cause="x", fix_strategy="y", confidence="low")
    hint = await _hint(buggy_repo, diag)
    assert hint and "grep" in hint
    assert hint.count("\n") < 12, f"不该是一堵路径墙：{hint!r}"


def test_the_hint_rides_along_with_the_diagnosis():
    """提示要真的进到开场白里，否则前面几条都白测。"""
    msgs = build_initial_messages(_FAILURE, _DIAG, locate="真身在 src/shopcart/cart.py")
    blob = "\n".join(str(m.content) for m in msgs)
    assert "真身在 src/shopcart/cart.py" in blob

    msgs = build_initial_messages(_FAILURE, None, locate="真身在 pkg/cart.py")
    blob = "\n".join(str(m.content) for m in msgs)
    assert "真身在 pkg/cart.py" in blob, "降级路径同样要带上"
