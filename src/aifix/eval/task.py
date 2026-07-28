"""评测任务与结果的数据模型。

任务集是一份 jsonl：一行一个任务，自带 ground truth —— gold_files 来自
那个把测试从红修到绿的 commit，不需要人来标注。

一个 target_test 一个任务（而不是一个 commit 一个任务），是为了让
TaskResult 保持规格 §9 的形状：单一 verdict、单一 attempts。一个 commit
修好多个测试时产出多个任务。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel


class Task(BaseModel):
    task_id: str
    repo: str                    # 源仓库绝对路径，评测从这里克隆
    commit: str                  # 把测试从红修到绿的那个 commit
    base_commit: str             # 它的父提交：源码要回到这里
    test_files: list[str]        # 从 commit 取来覆盖上去的测试文件
    target_test: str             # 期望被修绿的那一个用例
    gold_files: list[str]        # commit 里改动的源码文件 —— ground truth
    adapter: str = "pytest"
    # C 类（人造变异）任务：施加在 base_commit 之上的 unified diff。
    # 不在源仓库里建提交 —— 那会污染用户的仓库；补丁随任务集走，
    # 任务集是一份自包含的 jsonl
    mutation_diff: str | None = None
    # mined（从 git history 挖）| mutated（人造变异）。两者分布不同，
    # 成功率不能平均成一个数字，见 score.summarize_by_origin
    origin: str = "mined"


class TaskResult(BaseModel):
    task_id: str
    model: str
    locate_hit: bool             # suspect_file ∈ gold_files（规格 §9 的定义）
    suspect_file: str | None
    verdict: str
    attempts: int
    tokens: int
    cost_usd: float
    violations: int
    abort_reason: str | None = None
    # 任务本身跑挂了（克隆失败、baseline 没复现……）。与「没修好」是两回事：
    # 前者是评测的问题，后者是被测系统的成绩，混在一起会污染成功率。
    error: str | None = None
    # mined | mutated，跟随对应 Task.origin 走，供 score.summarize_by_origin
    # 分开统计
    origin: str = "mined"
    # 补丁合理性静态信号的条数（见 aifix.signals）。不改判定，只标注
    signals: int = 0


M = TypeVar("M", bound=BaseModel)


def write_jsonl(path: Path, items: Iterable[BaseModel]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(it.model_dump_json() + "\n")


def read_jsonl(path: Path, model: type[M]) -> list[M]:
    out: list[M] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(model.model_validate_json(line))
    return out
