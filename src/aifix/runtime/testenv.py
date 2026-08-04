"""跑目标项目的测试时，剥掉 aifix 自己的配置环境。

**这是实测逼出来的**（2026-07-30，issue #2 的真跑）：workflow 给
`aifix issue handle` 设了 `AIFIX_PRICE_MAP` / `AIFIX_BUDGET_CNY` /
`AIFIX_FIXER__MODEL` 等，它们随进程环境一路传进了**目标项目的 pytest 子进程**。
目标项目恰好是 aifix 自己，它的 `AifixConfig` 读的正是这些变量 —— baseline 里
凭空多出 15 个红（`test_config::test_defaults`、整个 `test_cost_gate_e2e`……）。

后果不止是噪音：

- 那些红会**进队列当成待修的 bug**，模型可能被派去修 aifix 自己造成的污染
- 「这个补丁没弄坏别的」这个判定，是在一个被自己弄脏的对照组上做的
- 每一轮 baseline 与 verify 都要多跑它们，白花时间

为什么不改 `os.environ`：评测会并行跑几十个任务（`eval --parallel`），进程级
的全局状态在那里是竞态。命令层的 `env -u` 是**每次调用各自独立**的。

为什么不用 `env -i`（清空）：目标项目的测试普遍依赖 `PATH` / `HOME` /
`VIRTUAL_ENV`，清空等于让它直接跑不起来。只剥自己那一撮，别碰别人的。
"""
from __future__ import annotations

import os


def aifix_vars_in_env(env: dict[str, str] | None = None) -> list[str]:
    """当前环境里所有 `AIFIX_` 开头的变量名，排序后返回。

    排序是为了让拼出来的命令**可复现** —— 同样的环境每次得到同样的 argv，
    否则 trace 里两次同样的调用长得不一样，复盘时会以为发生了别的事。
    """
    return sorted(k for k in (env if env is not None else os.environ)
                  if k.startswith("AIFIX_"))


def sanitized_command(command: list[str],
                      env: dict[str, str] | None = None) -> list[str]:
    """给测试命令套一层 `env -u`，剥掉所有 `AIFIX_` 变量。

    没有可剥的就**原样返回**，不加空的 `env` 前缀：无谓地包一层的代价是真实的
    —— 报错信息、进程树、`ps` 里看到的东西都多一层，而排查「测试怎么跑不起来」
    时那一层正好挡在最前面。
    """
    names = aifix_vars_in_env(env)
    if not names:
        return list(command)
    prefix: list[str] = ["env"]
    for n in names:
        prefix += ["-u", n]
    return prefix + list(command)
