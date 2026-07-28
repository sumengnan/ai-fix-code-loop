"""从 git history 挖任务集。

规格 §9 的做法：
    找出让测试从红变绿的 commit C
    任务 = checkout 到 C^，但保留 C 中的测试文件
    期望 = agent 的补丁让该测试转绿且不引入回归
    对照 = C 中的源码改动即标准答案

自带 ground truth，分布真实 —— 不需要人来标注，也不会像人造变异那样
在分布上跑偏。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

from ..adapters.pytest_adapter import PytestAdapter
from ..nodes.baseline import run_full_suite, run_scoped
from .task import Task
from .workspace import materialize


def split_paths(paths: list[str],
                test_dirs: list[str]) -> tuple[list[str], list[str]]:
    """把 commit 改动的路径拆成（测试侧, 源文件）。

    「测试侧」不只是 .py：测试目录下的夹具（数据文件、快照、配置片段）必须
    跟着测试一起被 materialize 嫁接。少了它们，任务在 base 侧因缺文件而红、
    在 C 侧绿，通过全部现有校验进入任务集，但 ground truth 实际不可达 ——
    修复模型即便诊断和补丁都对也过不了。这不是捏造任务（确实是红转绿），
    是任务质量问题。

    非测试目录下的非 .py 一律忽略，**不进 gold_files**：gold 是 locate_hit
    的判定依据，衡量的是 Detector 定位**源文件**的能力，塞进数据文件会稀释
    这个指标。
    """
    tests: list[str] = []
    src: list[str] = []
    # 按**分段**比前缀，不用裸 startswith：`testdata/x.py`.startswith("test")
    # 是 True，但它不是 `test` 目录下的文件
    prefixes = [PurePosixPath(d).parts for d in test_dirs]
    for p in paths:
        pp = PurePosixPath(p)
        in_test_dir = any(pp.parts[:len(d)] == d for d in prefixes if d)
        # conftest.py 可能躺在仓库根目录 —— 既不在 test_dirs 里，也不以
        # test_ 开头，会被判成源文件进 gold_files。它是测试基础设施。
        if in_test_dir or pp.name == "conftest.py" or pp.name.startswith("test_"):
            tests.append(p)
        elif pp.suffix == ".py":
            src.append(p)
    return tests, src


def is_candidate(test_files: list[str], gold_files: list[str]) -> bool:
    """同时动了测试与源码才可能是「红转绿」。

    只动测试 → 没有 gold；只动源码 → 没有判定用的 oracle。
    """
    return bool(test_files) and bool(gold_files)


def _git(repo: str | Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()}")
    return res.stdout


def _changed_paths(repo: str, commit: str) -> list[str]:
    """本次 commit 改动的路径。

    `--diff-filter=d`（小写 d = 排除删除）很关键：被删除的路径同样会被
    `--name-only` 列出来，若它是个测试文件就会进 test_files，随后
    materialize 的 `git checkout <C> -- <已删除路径>` 必然报 pathspec
    不匹配，整个挖掘就在那个 commit 上崩掉。真实仓库里删测试是常事。
    """
    out = _git(repo, "show", "--name-only", "--diff-filter=d",
               "--pretty=format:", commit)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _parent(repo: str, commit: str) -> str | None:
    """根提交没有父提交 —— 返回 None 而不是抛。"""
    try:
        return _git(repo, "rev-parse", f"{commit}^").strip()
    except RuntimeError:
        return None


async def verify_commit(repo: str, commit: str, base_commit: str,
                        test_files: list[str], adapter: PytestAdapter,
                        workdir: Path) -> list[str]:
    """返回「在 C^ 处红、在 C 处绿」的用例；不成立返回 []。

    四个阶段：scoped 到 test_files 取红、scoped 到 test_files 取绿、
    scoped 到候选本身复跑确认、最后回到 C^ 状态跑一次全量确认。红转绿的
    判定只需要本次 commit 动过的那几个测试文件，跑全量是白花时间 ——
    实测本仓库单次全量 171 秒，一个候选 commit 两次全量约 6 分钟，而候选
    在真实仓库里占提交总数的六成以上，这是把评测规模做上去的最大阻碍。

    任何一次测试没能产出报告都抛 RuntimeError（require_report=True）：
    任务集是要被反复使用的 ground truth，宁可跳过一个 commit，也不能
    把一批假任务写进去 —— 假任务不会报错，只会让所有模型的修复成功率
    一起变低，看起来像「模型都不行」。

    阶段 3 复跑候选用例时，传给 pytest 的不是原生 nodeid，而是
    adapter.make_test_id 从 junit 报告合成出来的 id。无效 id 的代价不是
    报错而是静默：pytest 在收集阶段整轮中止（exit code 4），连一个用例都
    没跑，写出的是一份 tests="0" 的空报告 —— 报告文件存在，require_report
    检查不出异常，看起来像「全部复跑通过」。

    所以不能只看 `cand - recheck.ids`（复跑后不在失败集里的）就当成过关：
    必须先用 recheck.ran 确认这个用例真的被 pytest 跑到过，跑不到的
    候选（包括这种整轮中止的情形）一律不能入选，而不是被这条静默路径
    误判成「复跑绿了」全部放行。
    """
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    materialize(repo, base_commit, commit, test_files, workdir)
    # materialize 之后的 HEAD 就是「C^ 源码 + C 测试」这个状态。它可能是
    # base_commit 本身（测试无差异时不建提交），也可能是 materialize 新建的
    # 那个提交 —— 阶段 4 要回到这里，所以现在就记下来
    staged_head = _git(workdir, "rev-parse", "HEAD").strip()

    # test_files 含测试目录下的非 .py 夹具（见 split_paths），它们能被
    # materialize 嫁接，但不能出现在 pytest 命令行上
    scope = [p for p in test_files if PurePosixPath(p).suffix == ".py"]
    if not scope:
        return []
    red = await run_scoped(workdir, adapter, scope, require_report=True)
    if not red.ids:
        return []
    _git(workdir, "checkout", "--force", "--quiet", commit)
    green = await run_scoped(workdir, adapter, scope, require_report=True)

    # 交 green.ran 而不是只做差集：一个用例在 C 处被删掉或被跳过时，同样
    # 不会出现在 green.failures 里，但那不是「红转绿」——拿它当任务，
    # 任何模型都不可能通过，成功率被白白拉低。
    cand = (red.ids - green.ids) & green.ran
    # 文件级 id（收集错误）：green 侧该文件正常收集，发出的是各个用例，
    # 文件级 id 本身不会出现在 green.ran 里 —— 光靠上面那行会把「测试文件
    # 在 C^ 导入失败、在 C 正常」整类候选静默丢掉。实测本仓库 65 个候选
    # commit 里 32 个新增了测试文件，那正是这一类。
    cand |= {i for i in (red.ids - green.ids)
             if "::" not in i and _file_went_green(i, green)}
    if not cand:
        return []
    # 再单跑一遍这几个用例：顺序依赖与状态污染会让「碰巧这一次绿了」混进来。
    # 这一步很便宜（只跑几个用例），而一个误判会污染此后每一轮评测。
    recheck = await run_scoped(workdir, adapter, sorted(cand),
                               require_report=True)
    # 只认真的跑到了的候选。少了这一步，一旦 make_test_id 对某个候选合成出
    # 无效路径导致 pytest 整轮中止，recheck.ids 会是空集，
    # `cand - recheck.ids` 就把整批候选误判成复跑全绿而放行。
    cand = {i for i in cand
            if (_file_went_green(i, recheck) if "::" not in i
                else (i in recheck.ran and i not in recheck.ids))}
    if not cand:
        return []
    # 阶段 4：回到 C^ 状态跑一次全量。评测时 run_task 是用全量 baseline
    # 复现 target_test 的，scoped 下红、全量下绿的用例（顺序依赖、状态
    # 污染）到那时会变成 error —— 安全，但白跑一次模型。把这份浪费挪到
    # 这里一次性付清
    _git(workdir, "checkout", "--force", "--quiet", staged_head)
    red_full = await run_full_suite(workdir, adapter, require_report=True)
    return sorted(cand & red_full.ids)


def _file_went_green(file_id: str, fs) -> bool:
    """文件级 id 在 fs 这一侧「该文件的用例至少跑到一个且全部通过」。

    要求「至少跑到一个」而不只是「没有失败」：文件根本没被收集时同样
    没有失败，那不是变绿。
    """
    cases = {i for i in fs.ran if i.startswith(file_id + "::")}
    return bool(cases) and not (cases & fs.ids)


async def mine_tasks(repo: str, adapter: PytestAdapter, limit: int = 50,
                     max_tasks: int = 10, workdir: Path | None = None,
                     on_progress=None) -> list[Task]:
    """扫最近 limit 个提交，产出至多 max_tasks 个任务。

    on_progress(sha, n, error)：n≥0 是该 commit 产出的可用用例数；
    n=-1 且 error 非空表示这个 commit 验证失败被跳过。两者必须能分开 ——
    「这个 commit 没有可用用例」是正常结果，「验证跑挂了」是要去看的问题。
    """
    workdir = Path(workdir or Path(repo) / ".aifix" / "mine")
    workdir.mkdir(parents=True, exist_ok=True)
    name = Path(repo).name
    tasks: list[Task] = []

    shas = _git(repo, "log", "--no-merges", "--format=%H",
                f"-n{limit}").split()
    for sha in shas:
        if len(tasks) >= max_tasks:
            break
        base = _parent(repo, sha)
        if base is None:
            continue
        test_files, gold_files = split_paths(
            _changed_paths(repo, sha), adapter.test_dirs())
        if not is_candidate(test_files, gold_files):
            continue
        try:
            targets = await verify_commit(repo, sha, base, test_files,
                                          adapter, workdir / sha[:8])
        except RuntimeError as e:
            # 单个 commit 验证失败（报告缺失、git 操作失败）不该带走整轮挖掘：
            # `--limit 200` 跑几十分钟，为一个 commit 全盘作废没有道理。
            if on_progress:
                on_progress(sha, -1, str(e))
            continue
        if on_progress:
            on_progress(sha, len(targets), None)
        for t in targets:
            if len(tasks) >= max_tasks:
                break
            tasks.append(Task(
                task_id=f"{name}@{sha[:8]}::{t}",
                repo=str(Path(repo).resolve()), commit=sha, base_commit=base,
                test_files=test_files, target_test=t, gold_files=gold_files,
                adapter=adapter.name))
    shutil.rmtree(workdir, ignore_errors=True)
    return tasks
