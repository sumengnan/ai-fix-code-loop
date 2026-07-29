# 评测产物

这里放的是**真跑出来**的任务集与结果，不是示例。

| 文件 | 是什么 |
|---|---|
| `tasks-ai-harness-framework.jsonl` | 12 个任务，`aifix mine` 从 [ai-harness-framework](https://github.com/sumengnan/ai-harness-framework) 的 git history 挖出来的（`--limit 60 --max-tasks 12`）。每个任务自带 ground truth：`gold_files` 是那个把测试从红修到绿的 commit 改过的源文件 |
| `results-deepseek-v4-flash.jsonl` | 用 `deepseek-v4-flash` 跑一轮的逐任务明细 |

## 为什么目标仓库不是 aifix 自己

评测的每个任务要跑 1 次 baseline 全量 + 至多 3 次 verify 全量。aifix 自己的套件
384~678 秒，一个任务就要半小时以上；框架的套件 13 秒。

## 复现

```bash
cd /path/to/ai-harness-framework
uv run --with-editable /path/to/ai-fix-code-loop aifix eval \
    /path/to/ai-fix-code-loop/evals/tasks-ai-harness-framework.jsonl \
    --label deepseek-v4-flash --parallel 3 \
    --budget-per-task 0.60 --budget-total 6.00
```

`--with-editable` 那一层是必须的：aifix 用 `sys.executable` 跑目标项目的测试，
而 aifix 自己的 venv 装不了框架的测试依赖。这是一条真实的产品限制。

任务集里的 `repo` 字段是绝对路径，换机器要改。
