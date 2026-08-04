"""补丁必要性反查：把补丁拆成一个个单位，逐个反向，看目标用例还绿不绿。

守卫查的是 agent 的**行为**（改没改测试、diff 大不大、越没越界），
`signals.py` 查的是补丁的**形状**（删没删公开符号、加没加模块级状态）。
这一层查的是第三件事：补丁里的每一处改动，**对修好目标用例到底有没有贡献**。

起因是真实验收里那个形状 —— 模型修好了目标用例，顺手删掉了没有测试覆盖的
`mul`。`removed_public_symbols` 抓住了那一次，但它只认「删除公开符号」这一种
形状；改一个无关函数的返回值、新建一个谁也没 import 的 helper、把一段代码搬个
家，三条静态信号一条都不亮。而反查不看形状：撤掉它，目标照样绿，它就是多余的。

**这一层不改变判定，也不改变交付内容。** 三态判定仍然只看测试结果，查出来的
单位原样留在补丁里，只在报告的「值得多看一眼」里报出来。自动剔除是另一个量级
的改动 —— 剔完的补丁是一个**没有被验证过的新补丁**，要重跑全量才敢交付，而那
时判定就得依赖「剔除」这个动作的正确性，判定面就被这一层污染了。

## 已知偏差：只重跑目标用例

反向一个单位之后只跑**目标那一条**用例（`run_scoped`），不跑全量。于是这里
回答的严格来说是「**对目标用例**必要吗」，不是「必要吗」。误报的形状是真实
存在的：模型改对了 `calc.py`，同时改了 `api.py` 的调用点以免打破另一条用例
—— 撤掉调用点那一处，目标照样绿，它会被报出来，而它其实是必要的。

不跑全量是成本决定的：每个单位一次全量套件，一个 5 个 hunk 的补丁要跑 5 遍。
`filter_flaky` 敢做重跑正是因为它走的是 scoped，这里沿用同一条理由。

代价必须让读报告的人知道，所以报告里那一节的文案明写「只按目标用例判」。
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .adapters.base import FailureSet

# `@@ -旧起,旧长 +新起,新长 @@`。长度可省（省略等价于 1）。
# 取的是**新**那一侧：工作区里现在躺着的是打完补丁的文件，标签要指给人看的
# 行号也是那一份里的行号。
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_FILE_RE = re.compile(r"^\+\+\+ b/(.*)$")
_OLD_FILE_RE = re.compile(r"^--- a/(.*)$")


@dataclass(frozen=True)
class Unit:
    """反查的一个单位。

    patch 为 None 表示「整体新增的文件」—— 它不在 `git diff` 里（未跟踪），
    反向的方式是把文件删掉，而不是打一条反向补丁。
    """
    label: str          # 给人看的定位，如 `calc.py:12-18`
    path: str           # 仓库相对路径
    patch: str | None   # 可单独 `git apply -R` 的完整补丁；None = 整体新增


def _git(cwd: Path, *args: str, stdin: str | None = None):
    return subprocess.run(["git", *args], cwd=cwd, input=stdin,
                          capture_output=True, text=True)


def _label(path: str, header: str) -> str:
    m = _HUNK_RE.match(header)
    if not m:
        return path
    start = int(m.group(1))
    count = 1 if m.group(2) is None else int(m.group(2))
    # count 为 0 是纯删除：新文件里那个位置没有行，给单个行号而不是空区间
    return f"{path}:{start}" if count <= 1 else f"{path}:{start}-{start + count - 1}"


def _split_one_file(section: list[str]) -> list[Unit]:
    """把一个文件的 diff 段落拆成若干 Unit。

    段落形如：
        diff --git a/x b/x
        index …            ← 头部，每个 hunk 的补丁都要原样带上
        --- a/x
        +++ b/x
        @@ … @@            ← 从这里开始是第一个 hunk
        …
        @@ … @@            ← 第二个 hunk

    头部必须逐字复制进每一条补丁：`git apply` 靠 `--- / +++` 认文件，
    少了它整条补丁无效 —— 而 `git apply` 的拒绝是静默的（那个单位被跳过），
    表现出来和「这个补丁没有多余改动」一模一样。
    """
    head: list[str] = []
    hunks: list[list[str]] = []
    for line in section:
        if _HUNK_RE.match(line):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            head.append(line)
    if not hunks:
        # 二进制文件（`Binary files … differ`）、纯 mode 变更、重命名而内容没
        # 动 —— 都没有 hunk。反查对它们无话可说，跳过而不是编一个单位出来。
        return []

    path = ""
    for line in head:
        m = _FILE_RE.match(line)
        if m and m.group(1) != "dev/null":
            path = m.group(1)
            break
        m = _OLD_FILE_RE.match(line)
        if m and m.group(1) != "dev/null":
            path = m.group(1)
    if not path:
        return []

    out: list[Unit] = []
    for hunk in hunks:
        text = "\n".join([*head, *hunk])
        # 结尾必须有换行：`git apply` 对缺末尾换行的补丁会报 corrupt patch。
        out.append(Unit(label=_label(path, hunk[0]), path=path,
                        patch=text + "\n"))
    return out


def plan(diff: str, new_files: Sequence[str]) -> list[Unit]:
    """把 `git diff` 的输出 + 未跟踪的新文件，拆成可逐个反向的单位。

    new_files 单独传进来，不是从 diff 里认的：`git diff` 看不见未跟踪文件，
    而新建一个源文件是完全合法的修复（`Worktree.commit` 的 docstring 里记着
    同一条）。整个新文件算一个单位 —— 按行拆它没有意义，问题是「这个文件该
    不该存在」。
    """
    units: list[Unit] = []
    section: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if section:
                units += _split_one_file(section)
            section = [line]
        elif section:
            section.append(line)
    if section:
        units += _split_one_file(section)

    units += [Unit(label=f"{p}（整个新增文件）", path=p, patch=None)
              for p in new_files]
    return units


def _snapshot(path: Path) -> bytes | None:
    """文件当前内容；不存在返回 None（补丁删掉了它，或它就是新增的）。"""
    return path.read_bytes() if path.is_file() else None


def _restore(path: Path, blob: bytes | None) -> None:
    if blob is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)


def _revert(worktree: Path, unit: Unit) -> bool:
    """把这一个单位从工作区撤掉，返回是否撤成功。

    补丁那条走 `git apply -R`：hunk 头里的新侧行号说的正是工作区当前这一份
    （所有 hunk 都打上了）的行号，所以逐个反向不会因为别的 hunk 还在而错位。

    撤不掉就返回 False 让调用方跳过这一个 —— 一个撤不掉的单位没有结论可言，
    把它当成「必要」或「不必要」都是编的。
    """
    if unit.patch is None:
        target = worktree / unit.path
        if not target.is_file():
            return False
        target.unlink()
        return True
    return _git(worktree, "apply", "-R", "-", stdin=unit.patch).returncode == 0


async def unnecessary_changes(
    worktree: Path,
    diff: str,
    new_files: Sequence[str],
    target: str,
    rerun: Callable[[list[str]], Awaitable[FailureSet]],
    max_units: int,
) -> list[str]:
    """逐个反向，返回「撤掉之后目标用例照样绿」那些单位的标签。

    rerun：async callable，接收 test_id 列表、返回重跑后的 FailureSet ——
    与 `filter_flaky` 同一个形状，调用方传的是同一个 `run_scoped` 闭包。
    它必须带 `require_report=True`：报告缺失时返回空集合，而空集合在这里会被
    读成「目标绿了」，于是**每一个**单位都被报成不必要。不是没测出来，是把
    没测到当成了正证据。

    **保证工作区逐字还原**，包括 rerun 抛异常的路径（finally）。差一个字节，
    随后 commit 进交付分支的就不是被验证过的那个补丁，而这件事没有任何一处会
    出声。异常本身照抛给调用方：它说的是「这次测量不可信」，吞掉等于把它降级
    成一个假结论。
    """
    units = plan(diff, new_files)

    # 只有一个单位时不查：反向它就是把整个补丁撤掉，而「撤掉整个补丁目标就变
    # 红」是判 BETTER 的同义反复，一次 scoped 重跑换不到任何信息。绝大多数修复
    # 只有一个 hunk，这一条把这层的平均成本压到接近零。
    #
    # 超过上限整体跳过，不做「只查前 N 个」：半份名单在报告里读起来和完整名单
    # 一模一样，人会以为剩下的都查过且都必要。补丁大到这个地步本身就该亲眼看，
    # 而 `max_diff_lines` 那道守卫管的是更极端的规模。
    if len(units) <= 1 or len(units) > max_units:
        return []

    found: list[str] = []
    for unit in units:
        path = worktree / unit.path
        blob = _snapshot(path)
        try:
            if not _revert(worktree, unit):
                continue
            after = await rerun([target])
            if target not in after.ids:
                found.append(unit.label)
        finally:
            _restore(path, blob)
    return found
