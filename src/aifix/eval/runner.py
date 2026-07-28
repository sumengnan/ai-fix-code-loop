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
from ..signals import same_file
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

    分段后缀匹配本身在 `aifix.signals.same_file` —— 补丁合理性信号里的
    `files_outside_suspect` 问的是同一个问题「模型说的那个文件和我手上这
    个是不是同一个」。两边各留一份实现会各自漂移，届时同一对路径可能在定
    位准确率里算命中、在越界信号里算越界。
    """
    return any(same_file(suspect, gold) for gold in gold_files)


async def run_task(task: Task, config: AifixConfig, model: str, workdir: Path,
                   detector_client: Any = None,
                   fixer_client: Any = None) -> TaskResult:
    run_id = _safe_id(task.task_id)
    dest = Path(workdir) / run_id
    # origin 必须带上：这条 blank 是「baseline 未复现」分支的落点，若省
    # 略 origin 会退回 TaskResult 的默认值 mined，把变异任务的评测故障
    # 误记成挖掘任务的，恰好抵消掉按来源分行统计的意义。
    blank = TaskResult(task_id=task.task_id, model=model, locate_hit=False,
                       suspect_file=None, verdict="same", attempts=0,
                       tokens=0, cost_usd=0.0, violations=0,
                       origin=task.origin)

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
    # 按 fact 条数记，不按 value 展开：files_outside_suspect 的 value 是
    # 一个文件列表，若按列表长度算，「一次改动动了 20 个文件」会把这一列
    # 冲爆到几十，掩盖掉另外两类信号——三类各出没出现过一次，比出现的
    # 文件数量更能说明问题，且不会被单个信号的规模稀释或放大。
    signals = sum(1 for f in facts if f.get("key") in (
        "removed_public_symbol", "new_module_state", "files_outside_suspect"))
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
        # 有 results 行意味着中止发生在「verify 判定后」（better 或
        # attempt≥max_attempts 时写行）；**没有行恰恰说明这个 failure 压根
        # 没轮到**（在队列里但预算先耗尽、或被 only_test 过滤掉）。本分支加了
        # 「中止时补录在飞的 failure」之后，两轮之间的中止也恒有 results 行。
        # 回落到 0 恰好表达「没轮到」的含义，与「平均尝试」的计数基数一致。
        attempts=(row["attempts"] if row
                  else max(state.get("attempt", 0) - 1, 0)),
        tokens=state["spent_tokens"], cost_usd=state["spent_usd"],
        violations=violations,
        signals=signals,
        abort_reason=(row or {}).get("abort_reason") or state.get("abort"),
        origin=task.origin,
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


_SKIPPED = "整批预算耗尽，未运行"


def _blank(task_id: str, model: str, error: str, origin: str) -> TaskResult:
    # origin 没有默认值：跳过 / 异常两条路径各自持有对应的 task，写死
    # 一个默认值等于给「忘记传」留了退路——退路的另一头正是本函数存在的
    # 理由（被跳过/出错的变异任务落回默认 mined，统计上被并进挖掘任务）。
    return TaskResult(task_id=task_id, model=model, locate_hit=False,
                      suspect_file=None, verdict="same", attempts=0,
                      tokens=0, cost_usd=0.0, violations=0, error=error,
                      origin=origin)


async def run_suite(tasks: list[Task], config: AifixConfig, model: str,
                    workdir: Path, parallel: int = 4,
                    detector_client: Any = None,
                    fixer_client: Any = None,
                    on_done=None,
                    total_usd: float | None = None) -> list[TaskResult]:
    """并行跑整个任务集。返回顺序与传入顺序一致。

    total_usd：整批的美元上限。检查发生在**派发之前**，已经在跑的任务
    放它们跑完 —— 它们的结果是有效数据，中途掐掉等于白花已经花掉的钱。

    `spent` 记的不是「已经花掉」而是「已经发出去的最大可能花销」：算出
    `cap` 的同一把锁内立即把 `cap` 预留进 `spent`，任务跑完后再回填差额
    `cost_usd - cap`。若只在任务跑完后才累加（预留之前的写法），
    parallel=N 时 N 个并发槽位会在派发前全部读到同一个旧 `spent`，各自
    以为还有整批上限那么多额度可花 —— 实测 `total_usd=1.0`、每任务花
    `1.0`、4 个任务：parallel=1 正确地只花 $1.0，parallel=4 却花掉
    $4.0，4 倍超支，且 parallel=4 正是 `aifix eval` 的默认并发度。预留
    之后，整批的超支只可能来自「每个在跑的任务超出它自己那份额度的
    量」，而单任务的超出量由 `consume()` 的契约兜住（至多一次模型调
    用）。因此整批超支上界 = 并发数 × 一次模型调用的成本。

    这个上界有一个前提：**任务要么正常跑完、要么在没花钱之前就炸**。
    下面的异常路径按成本 0 全额退回预留（`r.cost_usd` 是 0.0，回填
    `0.0 - cap`）；若某个任务是花过钱之后才抛，那笔已经花掉的钱在
    `spent` 里就消失了，后续任务会拿着「以为还剩」的额度继续派发，整批
    实际花销可以超出上界，超出量正是丢掉的那部分。要收紧就得让
    `run_task` 在异常里也把已花销带回来，那是另一件事。
    """
    sem = asyncio.Semaphore(parallel)
    spent = 0.0
    lock = asyncio.Lock()

    async def one(t: Task) -> TaskResult:
        nonlocal spent
        async with sem:
            if total_usd is not None:
                skipped = False
                async with lock:
                    # 读 left、算 cap、预留进 spent 必须在同一把锁内一次
                    # 完成，中间不能释放锁 —— 否则另一个并发槽位会插进
                    # 来读到预留之前的旧 spent，竞态原样存在。锁内只判定
                    # 是否跳过，不做任何回调 —— on_done 是用户提供的 I/O
                    # 回调（CLI 里是 print），锁内调用会把整批调度阻塞在
                    # 一次 I/O 上，且与正常/异常两条路径的回调时机不一致。
                    left = total_usd - spent
                    if left <= 0:
                        skipped = True
                    else:
                        cap = min(config.budget_usd, left)
                        spent += cap
                if skipped:
                    # 记成 error 而不是失败的 verdict：这是评测的调度
                    # 决策，不是被测系统的成绩。混进比率分母会让修复
                    # 成功率凭空变低 —— 被测系统替调度背锅。
                    r = _blank(t.task_id, model, _SKIPPED, origin=t.origin)
                    if on_done:
                        on_done(r)
                    return r
                task_config = config.model_copy(
                    update={"budget_usd": cap})
            else:
                task_config = config
                cap = None
            try:
                r = await run_task(t, task_config, model, workdir,
                                   detector_client=detector_client,
                                   fixer_client=fixer_client)
            except Exception as e:      # 一个任务炸掉不能带走整个 suite
                r = _blank(t.task_id, model, repr(e), origin=t.origin)
            if cap is not None:
                # 回填预留与实际花销的差额。异常路径 r.cost_usd 是
                # 0.0，回填 0.0 - cap 会把预留原样退回，是对的 ——
                # 否则预留会永久占住额度，把后续任务全部饿死。
                async with lock:
                    spent += r.cost_usd - cap
            if on_done:
                on_done(r)
            return r

    return list(await asyncio.gather(*(one(t) for t in tasks)))
