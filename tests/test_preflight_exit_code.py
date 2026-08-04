"""preflight 拦下来的 run **必须退非 0**。

实测撞出来的（2026-08-01 的功能巡检）：

    $ aifix run /tmp/不存在的目录 ; echo $?
    **中止**：没有适配器认领这个项目：/tmp/不存在的目录
    0                                    ← 这个 0 是错的

`preflight_node` 只写 `abort`、从不写 `abort_kind`，而 `_cmd_run` 的退出码是
按 `abort_kind` 判的。于是「路径打错了」「不是 git 仓库」「工作区不干净」
这三种情况全部静默退 0。

**在流水线里这是最坏的一类失败**：`aifix run || echo 失败` 什么都不会打印，
CI 把它读成成功，而这次 run 一个用例都没跑过。这个仓库对同一类错误已经栽过
十次以上 —— 不报错、不崩溃，只有承诺是假的。

判据不是「有没有中止」，是**这次 run 到底跑没跑成**：

- 预算耗尽（tokens / cny / wall）→ **退 0**。活干到钱花完为止，结论仍然可信。
- preflight 拦下 / 收集错误 / 端点不通 / 崩了 → **退 1**。这次根本没跑成。

模型端点不通那一条早就是退 1 的，理由写的是「这不是修复失败，是这次 run 还
没开始就没跑起来」—— preflight 是同一个类别，而且更靠前。
"""
import subprocess
import sys
from pathlib import Path

import pytest

from aifix.config import AifixConfig
from aifix.graph import PREFLIGHT_ABORT_KIND, new_state
from aifix.nodes.preflight import preflight_node


def _run_cli(repo, *extra):
    """真的起一个进程，拿真的退出码 —— 这条测试的全部价值就在那个数字上。"""
    return subprocess.run(
        [sys.executable, "-m", "aifix.cli", "run", str(repo), *extra],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())})


# ── 单元：preflight 的三条拒绝路径都要带上 kind ──────────────────────────

def test_a_missing_repo_is_marked_as_a_preflight_abort(tmp_path):
    state = new_state(tmp_path / "根本不存在", AifixConfig(), run_id="t")
    out = preflight_node(state)
    assert out["abort"], "路径不存在却没有中止"
    assert out["abort_kind"] == PREFLIGHT_ABORT_KIND


def test_a_dirty_worktree_is_marked_as_a_preflight_abort(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n",
                                                  encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "tests" / "test_x.py").write_text("dirty\n", encoding="utf-8")

    out = preflight_node(new_state(tmp_path, AifixConfig(), run_id="t"))
    assert "工作区不干净" in (out["abort"] or "")
    assert out["abort_kind"] == PREFLIGHT_ABORT_KIND


def test_a_bad_test_python_is_marked_as_a_preflight_abort(tmp_path):
    cfg = AifixConfig(test_python="/绝对不存在的解释器")
    out = preflight_node(new_state(tmp_path, cfg, run_id="t"))
    assert "解释器" in (out["abort"] or "")
    assert out["abort_kind"] == PREFLIGHT_ABORT_KIND


def test_a_healthy_repo_carries_no_abort_kind(tmp_path):
    """反向对照：正常仓库不许被打上这个标记，否则每次 run 都退 1。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n",
                                                  encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    out = preflight_node(new_state(tmp_path, AifixConfig(), run_id="t"))
    assert out["abort"] is None
    assert out.get("abort_kind") is None


# ── 端到端：真起一个进程，看那个退出码 ──────────────────────────────────

@pytest.mark.parametrize("case", ["missing", "not-a-git-repo"])
def test_the_cli_really_exits_nonzero(tmp_path, case):
    """**这条才是真正要钉住的东西。**

    上面几条单元测试证明的是「字段设对了」，而线上出问题的是那个退出码 ——
    两者之间隔着 `_cmd_run` 里的一个集合，加了 kind 却忘了往集合里塞，
    症状和现在一模一样。
    """
    if case == "missing":
        repo = tmp_path / "不存在"
    else:
        repo = tmp_path / "notgit"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")

    res = _run_cli(repo)
    assert res.returncode != 0, (
        f"preflight 拦下来却退 0 —— 流水线会把它读成成功。\n{res.stdout}")
    # 报告仍然要印出来：退出码说明「这次没跑成」，报告说明「为什么」
    assert "中止" in res.stdout
