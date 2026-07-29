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
cd /path/to/ai-fix-code-loop
uv run aifix eval evals/tasks-ai-harness-framework.jsonl \
    --label deepseek-v4-flash --parallel 3 \
    --budget-per-task 0.60 --budget-total 6.00
```

在哪个目录、用谁的 venv 跑 `aifix eval` 都不影响目标项目的测试：每个任务的测试
解释器按它自己的 `repo` 字段（**源仓库**）解析 —— 显式的 `AIFIX_TEST_PYTHON` >
源仓库里的 `.venv/bin/python`（其次 `venv/`）> aifix 自己的解释器。所以这里的
前提只有一条：**任务集里 `repo` 指向的那个仓库，自己有一个装齐了测试依赖的
venv**（没有就显式配 `AIFIX_TEST_PYTHON`）。

（这一段以前写的是「必须套一层 `uv run --with-editable`，因为 aifix 用
`sys.executable` 跑目标项目的测试」。那是解释器解析做进来之前的复现方法，现在
不需要了，套着也没有坏处。）

任务集里的 `repo` 字段是绝对路径，换机器要改。
