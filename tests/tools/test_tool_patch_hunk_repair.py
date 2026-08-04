"""`@@` 里的行数由**正文**决定，不信模型报的那个数。

两次独立的实测撞上了同一个病灶：

  一次 run 里 15 次 apply_patch **12 次**栽在 `corrupt patch at line N`，
  326k tokens 修两个单行 bug（同样的活上一次只花 89k）。

  qwen3-coder-flash 的 39 任务评测：`apply_patch` 被调 332 次、**失败 309
  次**（93%），能解析的坏补丁里 **247/247 是表头行数与正文对不上 —— 100%**。

`corrupt patch` 不是「上下文对不上」，是**这个 diff 语法就坏的**。数行数正是
LLM 最不擅长的事，而**正文往往是对的**：模型陷在「read_file 431 次 /
apply_patch 332 次」的循环里烧完 50 万 token，一个字节都没改（13 个 token
耗尽的任务 `diff_lines` 全是 0）。

答案是 git 自己的 `--recount`（`_GIT_APPLY`），它造出来就是为了这件事
（「编辑过补丁但没调整 hunk 头」）。**曾经这里有一个手写的 `repair_diff`
在 Python 里重算表头**，两者在下面这两份真实样本上产出完全相同的判定 ——
四十行自己维护的代码换 git 的一个开关，不划算，所以删了。

下面两份 diff 是那次评测里**模型真实产出**的（不是手写的），保留原样。
"""
import subprocess

import pytest

from harness.sandbox.local import LocalSandbox
from harness.tools.base import ToolError

from aifix.checks.signals import under_dirs
from aifix.tools.patch import ApplyPatchTool, _apply_error


def _dirs(dirs):
    """目录列表 → `ProjectAdapter.is_test_path` 那种谓词。

    守卫从「收目录列表」改成「收谓词」（为了 vitest 的同目录布局）之后，
    这些用例各自在考的判断没有变。**逐个包、不统一换成
    `PytestAdapter().is_test_path`**：那会把只给 `["tests"]` 的用例悄悄放宽
    成 `["tests", "test"]`，考的东西被改掉了而测试照样绿。
    """
    return lambda p: under_dirs(p, dirs)


# 真实样本一：表头写 @@ -1,13 +1,13 @@，而正文实际是 15 个旧侧行 / 16 个新侧行
# —— **旧侧那个 13 也是错的**。注意 hunk 内部还夹着**裸空行**（前导空格丢了），
# 那是这批坏补丁里 28% 同时具备的第二种病。
_REAL_1 = """--- a/src/harness/state.py
+++ b/src/harness/state.py
@@ -1,13 +1,13 @@
 from __future__ import annotations

 from dataclasses import dataclass, field
-
 from .types import Message


 @dataclass
 class RunState:
     run_id: str
     messages: list[Message] = field(default_factory=list)
     step: int = 0
+    tokens_used: int = 0
+    wall_seconds_used: float = 0.0

     def append(self, message: Message) -> None:
         self.messages.append(message)
"""

# 真实样本二：@@ -39,12 +39,14 @@，正文 0 删 3 增 → 新侧应是 15
_REAL_2 = """--- a/src/harness/persistence/serialize.py
+++ b/src/harness/persistence/serialize.py
@@ -39,12 +39,14 @@ def runstate_to_dict(s: RunState) -> dict:
 def runstate_to_dict(s: RunState) -> dict:
     return {"run_id": s.run_id, "step": s.step,
             "messages": [message_to_dict(m) for m in s.messages]}
+    "tokens_used": s.tokens_used, "wall_seconds_used": s.wall_seconds_used}


 def runstate_from_dict(d: dict) -> RunState:
     st = RunState(run_id=d["run_id"])
     st.step = d["step"]
     st.messages = [message_from_dict(m) for m in d["messages"]]
+    st.tokens_used = d.get("tokens_used", 0)
+    st.wall_seconds_used = d.get("wall_seconds_used", 0.0)
     return st
"""


@pytest.mark.parametrize("sample", [_REAL_1, _REAL_2], ids=["样本一", "样本二"])
def test_git_stops_calling_these_corrupt(sample, tmp_path):
    """**核心断言，直接对 git 跑。**

    这两份补丁修表头之后**仍然会**因为上下文对不上被拒（正文本来就是模型
    瞎写的）—— 那正是我们要的：消灭机械故障之后，剩下的报错必须是真实的
    那一个。所以判据是「不再是 corrupt」，不是「能打上」。

    反向对照跑裸 `git apply`：不带 `--recount` 时它必须判 corrupt，否则这条
    测试什么都没证明（样本不够坏，或者 git 的行为变了）。
    """
    (tmp_path / "p.diff").write_text(sample, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def stderr_of(*flags):
        return subprocess.run(["git", "apply", "--check", *flags, "p.diff"],
                              cwd=tmp_path, capture_output=True,
                              text=True).stderr

    assert "corrupt patch" in stderr_of(), "样本不够坏，这条测试证明不了任何事"
    assert "corrupt patch" not in stderr_of("--recount")


def test_the_tool_actually_passes_recount(tmp_path):
    """**接线检查**：常量定义对了但没接上，是这个项目反复吃过的亏。

    check 与真正应用必须用同一组参数 —— 一个「dry run 通过、真跑失败」的
    工具比没有 dry run 更坏。
    """
    from aifix.tools.patch import _GIT_APPLY
    import inspect

    assert _GIT_APPLY == ["git", "apply", "--recount"]
    src = inspect.getsource(ApplyPatchTool.run)
    # 两处调用都要展开 _GIT_APPLY，不能有一处写死 ["git", "apply"]
    assert src.count("*_GIT_APPLY") == 2, src


async def test_a_diff_without_a_trailing_newline_still_applies(tmp_path):
    """结尾少一个 `\\n` 就被判 corrupt —— 与 `--recount` 治的是同一类问题：
    与修复正确性无关的形式要求。模型漏掉它是常事。"""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n",
                                      encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    sb = LocalSandbox(workspace=str(tmp_path))
    await sb.start()
    try:
        t = ApplyPatchTool(sb, is_test=_dirs(["tests"]))
        # 注意末尾**没有**换行
        await t.run(t.Params(diff=(
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a - b\n+    return a + b")))
        assert "a + b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
    finally:
        await sb.close()


def test_the_two_failures_get_different_advice():
    """**两种失败要给不同的建议**，混成一句会把模型指向走不通的路。

    实测里模型照着「你对文件当前内容的理解有误，请先 read_file 确认」去做，
    重读了 16 次同一个文件、grep 了 14 次，然后交出一份同样有结构问题的
    diff —— 那句建议把它带进了死循环。而语法坏掉的补丁，再读一百遍文件也
    不会变好。
    """
    malformed = _apply_error("error: corrupt patch at line 12")
    assert "重读文件没有用" in malformed
    assert "不用你数" in malformed          # 别让它再去数行数

    context = _apply_error("error: patch does not apply")
    assert "read_file" in context
    assert "重读文件没有用" not in context


@pytest.mark.parametrize("stderr", [
    "error: corrupt patch at line 12",
    "error: unrecognized input",
    "error: patch fragment without header at line 3",
])
def test_every_malformed_marker_is_recognised(stderr):
    """git 报「格式坏了」有好几种措辞。漏掉任何一种，那一类就会拿到
    「请先 read_file」—— 正是把模型送进死循环的那句话。"""
    assert "重读文件没有用" in _apply_error(stderr)


async def test_a_test_file_patch_is_still_refused(tmp_path):
    """`--recount` 放松的只是数数，**守卫一条都不许放松**。

    反向对照：这个开关让 git 更宽容，很容易顺手以为「宽容」是整体的。
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sb = LocalSandbox(workspace=str(tmp_path))
    await sb.start()
    try:
        t = ApplyPatchTool(sb, is_test=_dirs(["tests"]))
        with pytest.raises(ToolError, match="测试文件"):
            await t.run(t.Params(diff=(
                "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
                "@@ -1,1 +1,1 @@\n-assert False\n+assert True\n")))
    finally:
        await sb.close()
