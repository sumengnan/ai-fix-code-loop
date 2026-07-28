"""跑任务集：单任务执行与并行调度。

同进程 await run_once，不起子进程 —— 评测跑的必须是产品代码本身，
配置、trace、判定全都是同一套。崩溃隔离由「每个任务包一层 try」解决。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
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

    return TaskResult(
        task_id=task.task_id, model=model,
        # 规格 §9 的定义：对 ground truth 判，不是对 traceback 判
        locate_hit=suspect in task.gold_files if suspect else False,
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
