import subprocess

import pytest

from aifix.delivery import (COMMIT_EMAIL, COMMIT_NAME, Worktree,
                            ensure_clean)


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def test_ensure_clean_passes_on_clean_repo(buggy_repo):
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_ensure_clean_raises_on_dirty_repo(buggy_repo):
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="工作区不干净"):
        ensure_clean(buggy_repo)


def test_ensure_clean_ignores_aifix_dir(buggy_repo):
    """.aifix/ 是我们自己的产物目录，不能让它把第二次运行卡住。"""
    (buggy_repo / ".aifix" / "runs" / "old").mkdir(parents=True)
    (buggy_repo / ".aifix" / "runs" / "old" / "x.txt").write_text("x", encoding="utf-8")
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_worktree_created_on_new_branch(buggy_repo):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.path.is_dir()
        assert (wt.path / "calc.py").is_file()
        assert wt.branch == "aifix/abc123"
        branches = _git(buggy_repo, "branch", "--list", "aifix/abc123")
        assert "aifix/abc123" in branches


def test_main_worktree_untouched(buggy_repo, fixed_source):
    original = (buggy_repo / "calc.py").read_text(encoding="utf-8")
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
    assert (buggy_repo / "calc.py").read_text(encoding="utf-8") == original


def test_rollback_discards_changes(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.rollback()
        assert "a - b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_commit_keeps_changes_and_rollback_after_is_noop(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.commit("fix: test_add", paths=["calc.py"])
        wt.rollback()
        assert "a + b" in (wt.path / "calc.py").read_text(encoding="utf-8")


def test_has_changes_reflects_working_tree(buggy_repo, fixed_source):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.has_changes() is False
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        assert wt.has_changes() is True


def test_ensure_clean_ignores_untracked_files(buggy_repo):
    """未跟踪文件不该阻塞：worktree 从 HEAD 创建，它们根本进不去 agent 的工作区。

    真实运行中 __pycache__ 让整个工具直接不可用 —— 任何跑过一次测试
    且未把它加进 .gitignore 的项目都会被卡死。
    """
    (buggy_repo / "src").mkdir(exist_ok=True)
    (buggy_repo / "src" / "__pycache__").mkdir(parents=True)
    (buggy_repo / "src" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (buggy_repo / "scratch.md").write_text("随手笔记", encoding="utf-8")
    ensure_clean(buggy_repo)          # 不抛即为通过


def test_ensure_clean_still_blocks_tracked_modifications(buggy_repo):
    """已跟踪文件被改动仍须拦截：baseline 会和用户眼前的状态对不上。"""
    (buggy_repo / "calc.py").write_text("dirty", encoding="utf-8")
    (buggy_repo / "untracked.txt").write_text("noise", encoding="utf-8")
    with pytest.raises(RuntimeError, match="工作区不干净"):
        ensure_clean(buggy_repo)


def test_commit_only_stages_given_paths(buggy_repo, fixed_source):
    """构建产物不该进交付分支 —— 真实运行中 .pyc 被 git add -A 扫了进去。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        (wt.path / "junk.log").write_text("构建产物", encoding="utf-8")
        (wt.path / "__pycache__").mkdir()
        (wt.path / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        wt.commit("fix: x", paths=["calc.py"])
        tracked = _git(wt.path, "ls-tree", "-r", "--name-only", "HEAD")
        assert "calc.py" in tracked
        assert "junk.log" not in tracked
        assert "__pycache__/x.pyc" not in tracked


def test_commit_stages_new_file_when_listed(buggy_repo):
    """agent 新建的源文件必须能提交（它不在已跟踪集合里）。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "helper.py").write_text("def h():\n    return 1\n",
                                           encoding="utf-8")
        wt.commit("feat: helper", paths=["helper.py"])
        tracked = _git(wt.path, "ls-tree", "-r", "--name-only", "HEAD")
        assert "helper.py" in tracked


def test_file_at_head_reads_the_committed_version(buggy_repo, fixed_source):
    """改动还没提交时，旧内容只能从 HEAD 拿 —— 信号在 commit 之前计算。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        head = wt.file_at_head("calc.py")
        assert head is not None
        assert "a - b" in head          # HEAD 版本
        assert head != fixed_source     # 工作区版本不该混进来


def test_file_at_head_returns_none_for_new_file(buggy_repo):
    """补丁新建的文件在 HEAD 里不存在 —— 返回 None，signals 才不会误报删除。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "helper.py").write_text("def h():\n    return 1\n",
                                           encoding="utf-8")
        assert wt.file_at_head("helper.py") is None


def test_commit_with_empty_paths_is_noop(buggy_repo, fixed_source):
    """没有明确改过的文件就不该产生提交。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        before = _git(wt.path, "rev-parse", "HEAD").strip()
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        wt.commit("fix: x", paths=[])
        assert _git(wt.path, "rev-parse", "HEAD").strip() == before


def test_commit_raises_when_a_path_matches_nothing(buggy_repo):
    """`git add -- <匹配不到的路径>` 之后 commit 会失败 —— 不许静默。

    这是「不崩溃、不报错、测试全绿，只有承诺是假的」那个形状：add 匹配不到
    任何文件（历史上 touched 记的是 diff 头上带前缀的 `a/calc.py`），commit
    因无内容以 1 退出，而报告照写「修复 1/1」并给出 `git merge` —— 交付分支
    上一个提交都没有。
    """
    with Worktree(buggy_repo, run_id="abc123") as wt:
        before = _git(wt.path, "rev-parse", "HEAD").strip()
        with pytest.raises(RuntimeError):
            wt.commit("fix: x", paths=["a/calc.py"])
        assert _git(wt.path, "rev-parse", "HEAD").strip() == before


def test_commit_raises_when_only_some_paths_match(buggy_repo, fixed_source):
    """一条路径匹配不到就整体失败 —— 半个交付比没有交付更难发现。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        with pytest.raises(RuntimeError):
            wt.commit("fix: x", paths=["calc.py", "b/nope.py"])


def test_commit_is_quiet_when_the_files_equal_head(buggy_repo):
    """区分度：路径都在、内容与 HEAD 逐字相同 —— 这是合法的「无事可提交」。

    一个「commit 非 0 就抛」的实现会把这一条也判成失败。两者的分界在**暂存
    结果**：add 报错说明路径本身有问题，add 成功却什么都没暂存说明文件真的
    没变。
    """
    with Worktree(buggy_repo, run_id="abc123") as wt:
        before = _git(wt.path, "rev-parse", "HEAD").strip()
        wt.commit("fix: x", paths=["calc.py"])      # 一个字节都没改
        assert _git(wt.path, "rev-parse", "HEAD").strip() == before


def test_commit_returns_true_only_when_a_commit_was_really_produced(
        buggy_repo, fixed_source):
    """返回值就是「交付分支上到底多没多一个提交」—— 判定侧唯一能信的答案。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        before = _git(wt.path, "rev-parse", "HEAD").strip()
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        assert wt.commit("fix: x", paths=["calc.py"]) is True
        assert _git(wt.path, "rev-parse", "HEAD").strip() != before


def test_commit_returns_false_when_the_files_equal_head(buggy_repo):
    """补丁被自己的反向补丁抵消：路径都在、内容与 HEAD 逐字相同。

    这不是错误（所以不抛），但它**没有交付**，调用方必须能分辨 ——
    否则报告照写「已修复」，而交付分支上一个提交都没有。
    """
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.commit("fix: x", paths=["calc.py"]) is False


def test_commit_returns_false_with_empty_paths(buggy_repo):
    with Worktree(buggy_repo, run_id="abc123") as wt:
        assert wt.commit("fix: x", paths=[]) is False


def test_commit_returns_true_for_a_new_untracked_file(buggy_repo):
    """新建文件也是**真的交付了** —— 区分度：`git diff` 看不见未跟踪文件。

    拿 `git diff` 当「有没有改动」的判据，这一条会返回 False：模型新建一个
    源文件是完全合法的修复，却被判成「什么都没做」而降级、回滚。那比现在的
    洞更糟 —— 现在只是多报一次修复，那样是把真修复扔掉。
    """
    with Worktree(buggy_repo, run_id="abc123") as wt:
        (wt.path / "helper.py").write_text("def h():\n    return 1\n",
                                           encoding="utf-8")
        assert wt.has_changes() is False        # git diff 眼里「什么都没变」
        assert wt.commit("feat: helper", paths=["helper.py"]) is True
        assert "helper.py" in _git(wt.path, "ls-tree", "-r", "--name-only",
                                   "HEAD")


def test_commit_reports_the_offending_path(buggy_repo):
    """报错里必须带上那条路径，否则人拿到的是一句无从下手的「提交失败」。"""
    with Worktree(buggy_repo, run_id="abc123") as wt:
        with pytest.raises(RuntimeError, match="a/calc.py"):
            wt.commit("fix: x", paths=["a/calc.py"])


def test_commit_uses_an_explicit_aifix_identity(
        buggy_repo, fixed_source, monkeypatch):
    """交付提交的作者必须是 aifix 自己，不能靠环境里那份身份。

    不设身份时 git **不会失败**，它会从主机名推断一个出来 —— 实测（2026-07-29，
    macOS）得到 `苏梦楠 <sumengnan@MacBook-Pro-5.local>`；GitHub 的 runner 上
    会是 `runner@fv-az….(none)` 这一类。两者都是查无此人的地址，而这条提交是
    要出现在 PR 上给人看的。

    所以问题不是「提交不成功」，是**署名是假的**。顺带解决第二件事：那份推断
    依赖 GECOS 与主机名解析，在精简容器里会失败，那时 commit 的 RuntimeError
    会被 verify_node 接住、判定降级成 SAME、报告写「交付失败（git add 未能暂存
    改动）」—— 一句指向 git add 的话，而真相在 commit 那一步。
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")

    with Worktree(buggy_repo, run_id="ident") as wt:
        (wt.path / "calc.py").write_text(fixed_source, encoding="utf-8")
        assert wt.commit("fix: x", paths=["calc.py"]) is True

    who = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>", "aifix/ident"],
        cwd=buggy_repo, capture_output=True, text=True).stdout.strip()
    assert who == f"{COMMIT_NAME} <{COMMIT_EMAIL}>", who
