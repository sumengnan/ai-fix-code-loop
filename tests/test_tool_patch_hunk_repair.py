"""hunk 头的行数由**正文重算**，不信模型报的那个数。

实测（2026-07-31，qwen3-coder-flash 的 39 任务评测）：`apply_patch` 被调用
332 次，**309 次失败**，几乎全是 `corrupt patch at line N` —— 而 `corrupt patch`
不是「上下文对不上」，是**这个 diff 本身语法就坏的**：`@@ -a,b +c,d @@` 里的
行数与正文对不上。

数行数正是 LLM 最不擅长的事，而**正文往往是对的**。于是模型陷在
「read_file 431 次 / apply_patch 332 次」的循环里烧完 50 万 token，一个字节
都没改（13 个 token 耗尽的任务，`diff_lines` 全是 0）。

雪上加霜的是 aifix 给的提示：「通常说明你对文件当前内容的理解有误，请先
read_file」—— 那是给「does not apply」的建议，对语法坏掉的补丁毫无用处，
把模型指向了一条走不通的路。

下面两份 diff 是那次评测里**模型真实产出**的（不是手写的），保留原样。
"""
import pytest

from aifix.tools.patch import repair_diff

# 真实样本一：表头写 @@ -1,13 +1,13 @@，而正文实际是 15 个旧侧行 / 16 个新侧行
# —— **旧侧那个 13 也是错的**。这一点写测试时我自己先按「旧侧对、只调新侧」
# 推错了一次：正确答案只能由正文给，而那正是重算存在的理由。
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


def _headers(diff: str) -> list[str]:
    return [ln for ln in diff.splitlines() if ln.startswith("@@")]


def test_wrong_counts_are_recomputed_from_the_body():
    """答案由**正文**给：14 个上下文 + 1 删 + 2 增 → 旧 15 / 新 16。

    模型报的是 `-1,13 +1,13`，**两侧都错**。
    """
    out = repair_diff(_REAL_1)
    assert _headers(out) == ["@@ -1,15 +1,16 @@"], _headers(out)


def test_the_hunk_context_suffix_is_preserved():
    """`@@ ... @@ def foo():` 尾巴上那段函数名要留着 —— 它对 git 不是必需的，
    但人读 diff 时靠它定位，扔掉等于让复盘变难。"""
    out = repair_diff(_REAL_2)
    assert _headers(out)[0].endswith("@@ def runstate_to_dict(s: RunState) -> dict:")


def test_a_correct_header_is_left_alone():
    """反向对照：本来就对的不许被改动 —— 否则这个函数在「修坏补丁」的同时
    也在「改好补丁」，而后者是纯粹的风险。"""
    good = ("--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,4 @@\n a\n b\n+c\n d\n")
    assert repair_diff(good) == good


def test_no_content_is_ever_changed():
    """**安全边界**：允许给裸空行补一个前导空格，但不许改任何内容。

    顺手「修」正文等于替模型改代码，那是 apply_patch 的越权 —— 它只该判
    「这个补丁能不能打上」，不该参与「补丁写什么」。

    判据是逐行 rstrip 之后必须完全相同：补空格只影响空白，改内容一定会被抓到。
    """
    out = repair_diff(_REAL_1)
    a = [ln for ln in _REAL_1.splitlines() if not ln.startswith("@@")]
    b = [ln for ln in out.splitlines() if not ln.startswith("@@")]
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x.rstrip() == y.rstrip(), (repr(x), repr(y))
    # 而且**只有空行**被动过
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    assert all(x.strip() == "" for x, _ in changed), changed


def test_single_line_hunk_shorthand_is_understood():
    """`@@ -5 +5 @@`（省略计数）是合法写法，不能把它当坏的。"""
    d = "--- a/x\n+++ b/x\n@@ -5 +5 @@\n-old\n+new\n"
    assert _headers(repair_diff(d)) == ["@@ -5,1 +5,1 @@"]


def test_garbage_is_returned_unchanged_not_crashed():
    """认不出来的东西原样返回。

    这个函数只做一件确定的事；看不懂时**闭嘴让 git 去报错**，比自作主张
    改一版出来强 —— git 的报错至少是准的。
    """
    for junk in ("", "不是 diff", "--- a/x\n+++ b/x\n@@ 乱七八糟 @@\n a\n"):
        assert repair_diff(junk) == junk


@pytest.mark.parametrize("sample", [_REAL_1, _REAL_2])
def test_repaired_headers_match_what_git_expects(sample, tmp_path):
    """**真跑一次 git apply --check**，不只断言数字。

    手算的行数只能证明我们理解得自洽，证明不了 git 认。这两份补丁修表头之后
    仍然会因为「上下文对不上」被拒（正文本来就是模型瞎写的）——**这正是我们
    要的**：修掉机械故障之后，剩下的报错必须是真实的那一个。
    """
    import subprocess

    out = repair_diff(sample)
    (tmp_path / "p.diff").write_text(out, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    res = subprocess.run(["git", "apply", "--check", "p.diff"], cwd=tmp_path,
                         capture_output=True, text=True)
    # 关键：不再是 corrupt patch。是别的错（文件不存在 / 上下文不匹配）都行。
    assert "corrupt patch" not in res.stderr, res.stderr
