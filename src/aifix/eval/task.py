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

from pydantic import BaseModel, ValidationError


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
    cost_cny: float
    violations: int
    abort_reason: str | None = None
    # 任务本身跑挂了（克隆失败、baseline 没复现……）。与「没修好」是两回事：
    # 前者是评测的问题，后者是被测系统的成绩，混在一起会污染成功率。
    error: str | None = None
    # mined | mutated，跟随对应 Task.origin 走，供 score.summarize_by_origin
    # 分开统计
    origin: str = "mined"
    # 补丁合理性静态信号的条数（见 aifix.checks.signals）。不改判定，只标注
    signals: int = 0


M = TypeVar("M", bound=BaseModel)


def write_jsonl(path: Path, items: Iterable[BaseModel]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(it.model_dump_json() + "\n")


def read_jsonl(path: Path, model: type[M]) -> list[M]:
    """读一份 jsonl。**读不动时给一句指得到方向的话，不是裸 traceback。**

    一次功能巡检读数：`aifix eval /打错的/路径` 和
    `aifix eval-report /打错的/路径` 吐的都是 `FileNotFoundError` 的调用栈，
    而同一次巡检里 `aifix stats` 给的是「还没有灌过库：…先跑 aifix ingest」。
    同一个仓库里两套标准，而这个项目自己的说法是「最贵的失败一向不是崩溃，
    是指错方向的诊断」——一段 io.open 的栈帧连方向都没指。

    行号必须报出来：一份几十行的结果文件里坏了一行，不说是哪一行，人只能
    从头看起。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(
            f"文件不存在：{p}\n"
            f"  这里要的是一份 jsonl（每行一个 JSON 对象）。\n"
            f"  任务集由 `aifix mine` / `aifix mutate` 产出，"
            f"结果文件由 `aifix eval --out` 产出。") from None
    except IsADirectoryError:
        raise SystemExit(f"这是个目录，不是文件：{p}") from None
    except UnicodeDecodeError:
        raise SystemExit(f"{p} 不是 UTF-8 文本，读不了。") from None

    out: list[M] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(model.model_validate_json(line))
        except ValidationError as e:
            raise SystemExit(
                f"{p} 第 {n} 行不是合法的 {model.__name__}：\n"
                f"  {str(e).splitlines()[0]}\n"
                f"  这份文件应当由 aifix 自己产出；手工编辑过的话，"
                f"检查那一行是不是缺了字段或者被截断了。") from None
    if not out:
        # 空文件不是错误，但一定要说 —— 后面那句「0 个任务」看起来像正常收场，
        # 而真相多半是路径指错了另一个文件
        raise SystemExit(f"{p} 里没有任何记录（空文件）。")
    return out
