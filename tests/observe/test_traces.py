"""`aifix/traces` 孤儿分支：让 trace 活过 runner。

runner 是临时的。不主动持久化，`.aifix/runs/` 连同整台机器一起消失 —— 而
`ingest` / `stats` 那套跨 run 汇总扫的正是那个目录，在 Actions 上它下面永远
只有一个 run，跨 run 统计天然失效。

只推 facts + report，不推 events：这正是 trace.py 开头写下的那条区分 ——
**事实是结论，事件是原始素材**。前者要长期统计所以要永久，后者只在出问题时
才要，放 artifact（90 天）就够。
"""
import subprocess
from pathlib import Path

import pytest

from aifix.observe.traces import publish_traces
from aifix.observe.trajectory import ingest


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo_with_remote(buggy_repo, tmp_path):
    """一个带 bare 远端的仓库 —— 全程不联网。"""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(buggy_repo, "remote", "add", "origin", str(bare))
    _git(buggy_repo, "push", "-q", "origin", "main")
    return buggy_repo


def _make_run(repo: Path, run_id: str, verdict="better") -> None:
    d = repo / ".aifix" / "runs" / run_id
    d.mkdir(parents=True)
    (d / "facts.jsonl").write_text(
        '{"kind": "verdict", "value": "%s"}\n' % verdict, encoding="utf-8")
    (d / "report.md").write_text(f"# aifix run {run_id}\n", encoding="utf-8")
    # 体积大的那一份 —— 不该被推上去
    (d / "events.jsonl").write_text("x" * 10_000, encoding="utf-8")


def _files_on(repo: Path, branch: str) -> set[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", branch)
    return {ln for ln in out.splitlines() if ln.strip()}


def test_first_publish_creates_the_branch(repo_with_remote):
    _make_run(repo_with_remote, "aaa111")
    assert publish_traces(repo_with_remote, "aaa111") is True
    assert _files_on(repo_with_remote, "aifix/traces") == {
        "runs/aaa111/facts.jsonl", "runs/aaa111/report.md"}


def test_the_branch_is_an_orphan_not_a_copy_of_main(repo_with_remote):
    """孤儿分支上不能有源码。

    基于 main 建的话，每次 run 都会在一条永不合并的分支上复制一份完整源码树，
    仓库体积会随 run 数线性长；而这条分支的用途只是存几十行 jsonl。
    """
    _make_run(repo_with_remote, "aaa111")
    publish_traces(repo_with_remote, "aaa111")
    files = _files_on(repo_with_remote, "aifix/traces")
    assert not any(f.endswith(".py") for f in files), files
    parents = _git(repo_with_remote, "log", "--format=%P", "aifix/traces").strip()
    assert parents == "", "第一个提交必须没有父提交，否则它不是孤儿分支"


def test_events_are_not_published(repo_with_remote):
    """反向对照：目录里确实有 events.jsonl，是被**挑掉**的，不是本来就没有。"""
    _make_run(repo_with_remote, "aaa111")
    assert (repo_with_remote / ".aifix" / "runs" / "aaa111"
            / "events.jsonl").is_file()
    publish_traces(repo_with_remote, "aaa111")
    assert not any("events" in f
                   for f in _files_on(repo_with_remote, "aifix/traces"))


def test_a_second_run_is_appended_not_replaced(repo_with_remote):
    """这条分支是**累积**的 —— 覆盖掉历史等于把跨 run 统计的意义抹掉。"""
    _make_run(repo_with_remote, "aaa111")
    publish_traces(repo_with_remote, "aaa111")
    _make_run(repo_with_remote, "bbb222")
    publish_traces(repo_with_remote, "bbb222")
    files = _files_on(repo_with_remote, "aifix/traces")
    assert "runs/aaa111/facts.jsonl" in files
    assert "runs/bbb222/facts.jsonl" in files


def test_publishing_the_same_run_twice_is_harmless(repo_with_remote):
    """Actions 重跑同一个 job 是常事。第二次内容一模一样、无可提交 ——
    这不是错误，不能让它把整个 job 弄红。"""
    _make_run(repo_with_remote, "aaa111")
    publish_traces(repo_with_remote, "aaa111")
    assert publish_traces(repo_with_remote, "aaa111") is True


def test_a_run_without_facts_publishes_nothing(repo_with_remote):
    """没有 facts 就没有可统计的东西。为它建一条空提交只是噪音。"""
    (repo_with_remote / ".aifix" / "runs" / "empty").mkdir(parents=True)
    assert publish_traces(repo_with_remote, "empty") is False


def test_no_worktree_is_left_behind(repo_with_remote):
    """临时 worktree 不清理的话，下一次 run 会撞上「路径已被占用」而失败 ——
    而那个报错一个字都不会提到 trace 持久化。"""
    _make_run(repo_with_remote, "aaa111")
    publish_traces(repo_with_remote, "aaa111")
    listed = _git(repo_with_remote, "worktree", "list")
    assert "traces" not in listed, listed


def test_the_published_branch_can_be_ingested(repo_with_remote, tmp_path):
    """这条分支存在的**唯一理由**：clone 下来能直接 ingest。

    落不到这一步的话，前面每一条都只是在正确地搬运一堆没人能用的文件。
    """
    _make_run(repo_with_remote, "aaa111")
    _make_run(repo_with_remote, "bbb222")
    publish_traces(repo_with_remote, "aaa111")
    publish_traces(repo_with_remote, "bbb222")

    clone = tmp_path / "traces"
    subprocess.run(["git", "clone", "-q", "--branch", "aifix/traces",
                    str(repo_with_remote), str(clone)], check=True)
    assert ingest(tmp_path / "db", runs_dir=clone / "runs") == 2
