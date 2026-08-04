"""补丁必要性反查：拆 hunk、逐个反向、看目标用例还绿不绿。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aifix.adapters.base import Failure, FailureSet
from aifix.necessity import Unit, plan, unnecessary_changes

# 两个被改的地方隔了 8 行以上：git diff 默认带 3 行上下文，挨得近的改动会被
# 并成**一个** hunk，那样这份夹具就问不出「逐个反向」这件事了。
_ORIG = '''def add(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    return a / b


def helper():
    return 1
'''

# 两处改动：修好 add（真修复），改 helper 的返回值（与目标用例无关的改动）
_PATCHED = '''def add(a, b):
    return a + b


def mul(a, b):
    return a * b


def div(a, b):
    return a / b


def helper():
    return 2
'''


def _git(repo: Path, *args: str, stdin: str | None = None):
    return subprocess.run(["git", *args], cwd=repo, input=stdin,
                          capture_output=True, text=True)


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    """一个已提交 _ORIG、工作区是 _PATCHED 的 git 目录。"""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text(_ORIG, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "calc.py").write_text(_PATCHED, encoding="utf-8")
    return repo


def _diff(repo: Path) -> str:
    return _git(repo, "diff").stdout


def _fs(*ids: str) -> FailureSet:
    return FailureSet({
        i: Failure(test_id=i, classname="c", name="n", message="m", trace="t")
        for i in ids
    })


# —— plan()：把 diff 拆成可单独反向的单位 ——


def test_plan_splits_one_file_into_two_hunks(wt: Path):
    units = plan(_diff(wt), new_files=[])
    assert len(units) == 2
    assert all(u.path == "calc.py" for u in units)


def test_each_hunk_patch_reverses_on_its_own(wt: Path):
    """每一条单独的 patch 都要能被 `git apply -R` 打上去。

    这是整条反查的地基：拆出来的 patch 少一行头部、或者行号取错一侧，
    `git apply` 就会拒绝——而拒绝是**静默**的（那个 hunk 被跳过），
    表现是「查不出任何不必要的改动」，和真的没有不必要的改动一模一样。
    """
    for unit in plan(_diff(wt), new_files=[]):
        res = _git(wt, "apply", "-R", "-", stdin=unit.patch)
        assert res.returncode == 0, f"{unit.label} 反向失败：{res.stderr}"
        # 还原，好让下一个 hunk 在完整补丁的基础上反向
        (wt / "calc.py").write_text(_PATCHED, encoding="utf-8")


def test_label_carries_file_and_line_range(wt: Path):
    labels = [u.label for u in plan(_diff(wt), new_files=[])]
    assert all(lb.startswith("calc.py:") for lb in labels)
    assert labels[0] != labels[1]


def test_new_files_become_their_own_units():
    units = plan("", new_files=["helper.py"])
    assert len(units) == 1
    assert units[0].path == "helper.py"
    assert units[0].patch is None       # 整体新增，反向的方式是删掉它


def test_binary_diff_is_skipped():
    diff = ("diff --git a/x.bin b/x.bin\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/x.bin and b/x.bin differ\n")
    assert plan(diff, new_files=[]) == []


# —— unnecessary_changes()：反向、重跑、还原 ——


class _Rerun:
    """目标用例的替身：记下每次重跑时 calc.py 的内容，据此决定绿不绿。

    约定「只要 `return a + b` 还在，目标就绿」——于是反向掉修 add 的那个
    hunk 时目标变红（必要），反向掉 helper 那个 hunk 时目标仍绿（不必要）。
    """

    def __init__(self, path: Path, target: str) -> None:
        self.path = path
        self.target = target
        self.calls: list[list[str]] = []

    async def __call__(self, ids: list[str]) -> FailureSet:
        self.calls.append(list(ids))
        src = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        return _fs() if "return a + b" in src else _fs(self.target)


async def test_reports_only_the_unnecessary_hunk(wt: Path):
    rerun = _Rerun(wt / "calc.py", "t::target")
    labels = await unnecessary_changes(
        wt, _diff(wt), new_files=[], target="t::target",
        rerun=rerun, max_units=10)
    assert len(labels) == 1
    # 报出来的是 helper 那一处，不是修 add 那一处
    assert rerun.calls == [["t::target"], ["t::target"]]


async def test_worktree_is_restored_byte_for_byte(wt: Path):
    """最关键的不变量：反查跑完，工作区必须和跑之前逐字相同。

    差一个字节，随后 commit 进交付分支的就不是被验证过的那个补丁——
    而测试全绿、报告照写「已修复」，没有任何一处会出声。
    """
    before = (wt / "calc.py").read_bytes()
    await unnecessary_changes(wt, _diff(wt), new_files=[], target="t::target",
                              rerun=_Rerun(wt / "calc.py", "t::target"),
                              max_units=10)
    assert (wt / "calc.py").read_bytes() == before


async def test_single_unit_is_not_checked(wt: Path):
    """只有一个单位时不跑：反向它就是把整个补丁撤掉，问不出新东西。"""
    rerun = _Rerun(wt / "calc.py", "t::target")
    labels = await unnecessary_changes(
        wt, "", new_files=["helper.py"], target="t::target",
        rerun=rerun, max_units=10)
    assert labels == []
    assert rerun.calls == []


async def test_over_the_cap_skips_entirely(wt: Path):
    """超过上限整体跳过，不做「查前 N 个」——半份名单会被读成完整名单。"""
    rerun = _Rerun(wt / "calc.py", "t::target")
    labels = await unnecessary_changes(
        wt, _diff(wt), new_files=[], target="t::target",
        rerun=rerun, max_units=1)
    assert labels == []
    assert rerun.calls == []


async def test_restores_even_when_rerun_raises(wt: Path):
    """重跑抛异常（测试进程没跑起来）时，工作区照样要还原。

    异常本身照抛给调用方——`require_report=True` 抛出来的意思是「这次测量
    不可信」，吞掉它会让反查把「没测出来」读成「目标还绿」，进而把一个必要
    的 hunk 报成不必要。
    """
    before = (wt / "calc.py").read_bytes()

    async def _boom(ids: list[str]) -> FailureSet:
        raise RuntimeError("测试进程没有产出报告")

    with pytest.raises(RuntimeError):
        await unnecessary_changes(wt, _diff(wt), new_files=[],
                                  target="t::target", rerun=_boom,
                                  max_units=10)
    assert (wt / "calc.py").read_bytes() == before


async def test_new_file_unit_is_reverted_by_deleting_it(wt: Path):
    """整体新增的文件：反向 = 删掉它，还原 = 写回去。"""
    (wt / "helper.py").write_text("X = 1\n", encoding="utf-8")
    seen: list[bool] = []

    async def _rerun(ids: list[str]) -> FailureSet:
        seen.append((wt / "helper.py").is_file())
        return _fs()        # 一律绿：两个单位都会被报成不必要

    labels = await unnecessary_changes(
        wt, _diff(wt), new_files=["helper.py"], target="t::target",
        rerun=_rerun, max_units=10)
    assert False in seen                       # 有一轮它确实不在了
    assert (wt / "helper.py").read_text(encoding="utf-8") == "X = 1\n"
    assert any("helper.py" in lb for lb in labels)


def test_unit_is_hashable_and_frozen():
    u = Unit(label="a:1-2", path="a", patch="p")
    with pytest.raises(Exception):
        u.label = "b"                          # type: ignore[misc]
