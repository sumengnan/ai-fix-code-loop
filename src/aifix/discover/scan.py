"""把三层串起来：列候选 → 花钱确认 → 进修复循环。

**三级递进，每一级都要显式要**：

    aifix scan            零模型调用，只列候选
    aifix scan --probe    每个候选一次模型调用，确认哪些是真的
    aifix scan --fix      确认之后进修复循环（会改代码、会提交）

不做成一步到底，是因为这三级的代价差着数量级，而第一层是**启发式**的 ——
一份没确认过的候选清单直接进修复，等于对着一堆猜测花钱改代码。
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import ProjectAdapter
from ..config import AifixConfig
from ..runtime.delivery import COMMIT_EMAIL, COMMIT_NAME
from .probe import KIND_OK, ProbeOutcome, probe_twin
from .twins import find_twins

# 一次扫描最多探几个候选。每个候选是一次模型调用，而这一层最常见的结论是
# 「一致，作废」—— 没有上限的话，一个大仓库扫出几十对就是几十次调用换一份
# 空结论。
DEFAULT_MAX_PROBES = 5


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{res.returncode}）：{res.stderr.strip()}")
    return res.stdout


async def scan_and_fix(
    repo: Path | str,
    config: AifixConfig | None = None,
    adapter: ProjectAdapter | None = None,
    client: Any = None,
    run_fn: Callable[..., Any] | None = None,
    do_fix: bool = False,
    max_probes: int = DEFAULT_MAX_PROBES,
    min_shared: int = 3,
    on_note: Callable[[str], None] | None = None,
) -> list[ProbeOutcome]:
    """扫描 → 逐个探测 → （可选）把确认了的送进修复循环。

    只有 `KIND_OK`（确认两处不一致）才会进修复：`agreed` 是候选作废、
    `not_comparable` 是配错了对，两者都不是缺陷，去修它们等于修一个不存在的
    问题，而那会真花钱、真开 PR。
    """
    from ..adapters.pytest_adapter import PytestAdapter

    r = Path(repo)
    cfg = config or AifixConfig()
    ad = adapter or PytestAdapter()
    say = on_note or (lambda _s: None)

    twins = find_twins(r, min_shared=min_shared)
    say(f"扫到 {len(twins)} 对候选，探前 {min(len(twins), max_probes)} 对")

    outs: list[ProbeOutcome] = []
    for twin in twins[:max_probes]:
        say(f"── 探测 {twin.a.name} ↔ {twin.b.name}")
        # 每个候选一个 run_id：失败的对比测试会被删掉，trace 是唯一留下的现场。
        probe_id = uuid.uuid4().hex[:8]
        out = await probe_twin(r, ad, twin, config=cfg, client=client,
                               run_id=probe_id)
        say(f"   trace：.aifix/runs/{probe_id}/")
        outs.append(out)
        say(f"   {out.kind}：{out.reason}")

        if out.kind != KIND_OK:
            continue
        if not do_fix:
            continue

        rep = out.reproduction
        # 必须先 commit 再进循环：worktree 从 HEAD 建，baseline 才认得出这是一
        # 个失败用例。顺序反了的话队列是空的，run 以「没活干」正常收场退 0，
        # 而报告会说「你的仓库没问题」。
        _git(r, "add", "--", rep.test_file)
        _git(r, "-c", f"user.name={COMMIT_NAME}",
            "-c", f"user.email={COMMIT_EMAIL}", "commit", "-q", "-m",
            f"test: {twin.a.name} 与 {twin.b.name} 对同一输入结果不一致",
            "--", rep.test_file)

        fn = run_fn
        if fn is None:
            from ..cli import run_once
            fn = run_once
        say(f"── 开始修复：{rep.target_test_id}")
        await fn(r, cfg, run_id=uuid.uuid4().hex[:8],
                 only_test=rep.target_test_id)

    return outs
