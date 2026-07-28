"""跑任务集：单任务执行与并行调度。

同进程 await run_once，不起子进程 —— 评测跑的必须是产品代码本身，
配置、trace、判定全都是同一套。崩溃隔离由「每个任务包一层 try」解决。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from ..cli import run_once
from ..config import AifixConfig
from .task import Task, TaskResult
from .workspace import prepare_task_repo

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_id(task_id: str) -> str:
    """run_id 会变成分支名与目录名，必须先洗干净。

    截断后必须补一段哈希：mine 产出的 id 形如
    `proj@abc1234::很长的/路径/test_x.py::test_y`，同一文件里的两个用例
    只在尾部不同，光截断会撞成同一个 id —— 两个任务克隆到同一个目录，
    第二个 git clone 直接失败。
    """
    cleaned = _UNSAFE.sub("_", task_id).strip("_") or "task"
    digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:48]}_{digest}"


def _read_facts(repo: Path, run_id: str) -> list[dict[str, Any]]:
    p = Path(repo) / ".aifix" / "runs" / run_id / "facts.jsonl"
    if not p.is_file():
        return []
    return [json.loads(x) for x in
            p.read_text(encoding="utf-8").splitlines() if x.strip()]


def first_attempt_suspect(facts: list[dict[str, Any]]) -> str | None:
    """取第 1 轮 attempt 的 suspect_file；那一轮没写就返回 None。

    定位准确率量的是 Detector 的**冷启动**能力。第 2/3 轮已经看过上一轮的
    失败反馈，是一道更容易的题，混进来就不是同一个指标了。

    必须按 attempt 过滤而不是「取第一条 suspect_file」：detect_node 在 JSON
    解析失败时只写 diagnosis_parse_failed、不写 suspect_file，于是取第一条
    会静默滑到第 2 轮的诊断 —— 系统性抬高定位准确率，且抬高幅度正比于模型
    的 JSON 合规性有多差。跨模型对比里最不该被混淆的就是这个维度。
    """
    return next((f["value"] for f in facts
                 if f.get("key") == "suspect_file" and f.get("attempt") == 1),
                None)


def _path_parts(path: str) -> tuple[str, ...]:
    """把路径规整成 POSIX 分段序列，供后缀匹配用。

    仓库里的路径都是 git 产出的 POSIX 形式，但模型给出的 suspect_file 可能带
    `./` 前缀或 `\\` 分隔符（尤其是习惯 Windows 风格的模型）。先统一分隔符、
    去掉前导 `./`，再切分，两侧才能在同一套坐标系里比较。
    """
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).parts


def locate_hit(suspect: str | None, gold_files: list[str]) -> bool:
    """判定 suspect_file 是否定位到了 gold_files 里的某个文件。

    起因：M3 跨模型评测第一次真跑，deepseek-v4-pro 与 deepseek-v4-flash 的
    定位准确率都被判成 0%。看明细，两个模型给出的 suspect_file 是模块路径
    形式（`aifix/eval/mine.py`），gold_files 是仓库路径形式（`src/aifix/
    eval/mine.py`）——两者其实是同一个文件，只是少了一段 `src/` 前缀，旧的
    判定是裸字符串相等（`suspect in task.gold_files`），于是两个模型都答对
    了却都被计成没命中。定位准确率对应规格 §9 里 Detector 的能力，是跨模型
    对比的核心指标之一；如果判定依赖模型的路径书写习惯，这一列衡量的就不
    是定位能力，而是书写风格——习惯写模块路径的模型会被系统性地打成 0
    分，这在跨模型对比里是不能接受的。

    因此改成「路径分段后缀匹配」：两条路径各自按 `/` 切成分段序列，其中一
    个序列是另一个的后缀（不论谁更长），就算命中。

    为什么必须按分段比、不能用裸字符串 `endswith`：`"b/mine.py".endswith(
    "mine.py")` 和 `"xmine.py".endswith("mine.py")` 都是 True，但后者是把
    `"mine.py"` 从字符中间截出来的假阳性，`xmine.py` 根本不是同一个文件名。
    按分段比较，`xmine.py` 整段就不等于 `mine.py`，不会有这种误判。

    为什么不放宽到「只比文件名」：那样 `other/mine.py` 也会命中 `src/
    aifix/eval/mine.py`，仅仅因为文件名相同、目录完全对不上——指标会被
    「蒙对文件名」的运气稀释，跨模型对比就失去区分度了。只报裸文件名（不
    带任何目录）的情况之所以命中，是因为它本身就是「分段序列长度为 1 的
    后缀」，符合同一条规则，不是放宽出的特例。
    """
    if not suspect:
        return False
    suspect_parts = _path_parts(suspect)
    if not suspect_parts:
        return False
    for gold in gold_files:
        if not gold:
            continue
        gold_parts = _path_parts(gold)
        if not gold_parts:
            continue
        shorter, longer = ((suspect_parts, gold_parts)
                           if len(suspect_parts) <= len(gold_parts)
                           else (gold_parts, suspect_parts))
        if longer[len(longer) - len(shorter):] == shorter:
            return True
    return False


async def run_task(task: Task, config: AifixConfig, model: str, workdir: Path,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> TaskResult:
    run_id = _safe_id(task.task_id)
    dest = Path(workdir) / run_id
    blank = TaskResult(task_id=task.task_id, model=model, locate_hit=False,
                       suspect_file=None, verdict="same", attempts=0,
                       tokens=0, cost_usd=0.0, violations=0)

    prepare_task_repo(task, dest)
    state = await run_once(dest, config, run_id=run_id,
                           only_test=task.target_test,
                           detector_client=detector_client,
                           fixer_client=fixer_client)

    if task.target_test not in state["baseline_ids"]:
        # 任务失效（源仓库变了、环境不同、测试本身不稳定）。这与「没修好」
        # 是两回事 —— 混进成功率会让被测系统替评测的问题背锅。
        return blank.model_copy(update={
            "error": f"baseline 未复现目标用例：{task.target_test}"})

    facts = _read_facts(dest, run_id)
    suspect = first_attempt_suspect(facts)
    violations = sum(1 for f in facts if f.get("key") == "violation")
    row = next((r for r in state["results"]
                if r["test_id"] == task.target_test), None)

    result = TaskResult(
        task_id=task.task_id, model=model,
        # 规格 §9 的定义：对 ground truth 判，不是对 traceback 判。命中判定
        # 见 locate_hit() 的 docstring —— 裸字符串相等会把「模块路径 vs 仓
        # 库路径」这种书写风格差异算成没命中，跨模型对比里不能有这种偏差。
        locate_hit=locate_hit(suspect, task.gold_files),
        suspect_file=suspect,
        verdict=row["verdict"] if row else "same",
        # 没有 results 行 = 中止发生在两轮之间（verify_node 只在 better 或
        # attempt≥max_attempts 时才写行）。这时 state["attempt"] 停在「下一轮
        # 的编号」，真实跑过的轮数是它减一。落成 0 会把「平均尝试」系统性
        # 拉低，而这个任务明明真跑过。
        attempts=(row["attempts"] if row
                  else max(state.get("attempt", 0) - 1, 0)),
        tokens=state["spent_tokens"], cost_usd=state["spent_usd"],
        violations=violations,
        abort_reason=(row or {}).get("abort_reason") or state.get("abort"),
    )
    if state.get("abort_kind") == "wall":
        # 墙钟预算是评测调度器的属性，不是模型的属性：--parallel 8 时几个
        # 任务在同一台机器上抢 CPU 跑全量 pytest，墙钟耗尽的概率远高于
        # --parallel 1。记成模型的失败，就等于「只改并行度就能改变修复
        # 成功率」，直接违背跨模型对比的前提 —— 所以走 error，不进比率
        # 分母。token / 美元预算相反：同一批任务同一个上限，谁先烧完谁差，
        # 那是被测系统的真实成绩，仍记 verdict=same。
        return result.model_copy(update={
            "error": f"评测的墙钟预算耗尽（评测故障，非模型失败）："
                     f"{state.get('abort')}"})
    return result


async def run_suite(tasks: list[Task], config: AifixConfig, model: str,
                    workdir: Path, parallel: int = 4,
                    detector_client: Any = None,
                    fixer_client: Any = None,
                    on_done=None) -> list[TaskResult]:
    """并行跑整个任务集。返回顺序与传入顺序一致。"""
    sem = asyncio.Semaphore(parallel)

    async def one(t: Task) -> TaskResult:
        async with sem:
            try:
                r = await run_task(t, config, model, workdir,
                                   detector_client=detector_client,
                                   fixer_client=fixer_client)
            except Exception as e:      # 一个任务炸掉不能带走整个 suite
                r = TaskResult(task_id=t.task_id, model=model,
                               locate_hit=False, suspect_file=None,
                               verdict="same", attempts=0, tokens=0,
                               cost_usd=0.0, violations=0, error=repr(e))
            if on_done:
                on_done(r)
            return r

    return list(await asyncio.gather(*(one(t) for t in tasks)))
