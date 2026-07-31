"""待人回答的问题：落盘、读回、把编号翻成答案。

两条入口共用这一份**载荷格式**（`{run_id, repo, test_id, question, options}`），
但存的地方不一样，这是被运行环境逼出来的：

- **命令行**：存进 `.aifix/runs/<run_id>/pending.json`。同一台机器、同一个仓库，
  文件是最简单的持久层。
- **issue**：存进状态评论里的一个隐藏标记。Actions 的 job 是一次性的，容器
  连同磁盘一起消失 —— issue 本身才是那条流水线唯一活得够久的存储。

两边**必须是同一个 schema**：各存各的话，`options` 的编号从 0 还是从 1 数这种
事就会在两条路上分叉，而分叉的表现是「人回答了 2，机器按 3 去改」——不报错、
不崩溃，只是改错了地方。
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

PENDING_FILE = "pending.json"


def payload(run_id: str, repo: str, ask: dict[str, Any]) -> dict[str, Any]:
    """组一份待答载荷。`repo` 一定要带：`aifix answer` 是在**另一次进程**里
    跑的，它得知道回到哪个仓库去重跑。"""
    return {
        "run_id": run_id,
        "repo": str(repo),
        "test_id": ask.get("test_id", ""),
        "question": ask.get("question", ""),
        "options": list(ask.get("options") or []),
    }


def save(artifact_dir: str | Path, data: dict[str, Any]) -> Path:
    p = Path(artifact_dir) / PENDING_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def load(repo: str | Path, run_id: str) -> dict[str, Any] | None:
    p = Path(repo) / ".aifix" / "runs" / run_id / PENDING_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest(repo: str | Path) -> dict[str, Any] | None:
    """仓库里**最近**那个待答问题。

    让 `aifix answer 2` 不必先去查 run_id：问题是刚才那次 run 打印在屏幕上的，
    再要人回头翻一个哈希串出来，是把机器的记账负担转嫁给人。
    """
    root = Path(repo) / ".aifix" / "runs"
    best: tuple[float, dict[str, Any]] | None = None
    for p in root.glob(f"*/{PENDING_FILE}"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            mtime = p.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        if best is None or mtime > best[0]:
            best = (mtime, data)
    return best[1] if best else None


def clear(repo: str | Path, run_id: str) -> None:
    """答过就删掉。

    留着的话 `latest()` 会一直翻出这个陈旧的问题，而它已经被回答过了 ——
    人会看到自己刚答完的问题又被问一遍，还以为答案没送到。
    """
    Path(repo, ".aifix", "runs", run_id, PENDING_FILE).unlink(missing_ok=True)


def choose(data: dict[str, Any], choice: int) -> str:
    """把**从 1 数起**的编号翻成选项原文。越界抛 ValueError。

    从 1 数起是因为它是给人看的：屏幕上印的是「1. / 2. / 3.」，人回的是那个
    数字。这里的 off-by-one 不会崩溃，只会静静地按另一个选项去改代码 ——
    所以边界在这里判死，而不是让调用方各判各的。
    """
    options = data.get("options") or []
    if not 1 <= choice <= len(options):
        raise ValueError(
            f"编号 {choice} 超出范围：这个问题有 {len(options)} 个选项，"
            f"请回 1 到 {len(options)} 之间的数字。")
    return options[choice - 1]


# issue 那条路的持久层就是评论本身：Actions 的容器连同磁盘一起消失，
# `.aifix/` 下的东西活不过一次 job。
#
# **base64 而不是裸 JSON**：问题正文是模型写的自由文本，里面完全可能出现
# `-->`，那会当场把 HTML 注释截断 —— 后半截 JSON 直接显示在 issue 上，而
# 标记再也解析不出来。这不是理论风险：让模型描述一个跟注释语法有关的缺陷，
# 它就会写出来。
_MARKER = re.compile(r"<!-- aifix:ask ([A-Za-z0-9+/=]+) -->")


def encode_marker(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return f"<!-- aifix:ask {base64.b64encode(raw).decode('ascii')} -->"


def decode_marker(body: str) -> dict[str, Any] | None:
    """从评论正文里取回待答载荷。取不到返回 None —— 那表示没有问题在等，
    是个正常状态，不是错误。"""
    m = _MARKER.search(body or "")
    if m is None:
        return None
    try:
        data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("options") else None


def render(data: dict[str, Any]) -> str:
    """渲染成给人看的一段话。命令行与 issue 评论共用 —— 两边措辞不一样的话，
    人在两个地方读到的是两套东西，而它们描述的是同一个问题。"""
    lines = [f"**{data.get('question', '')}**", ""]
    for i, opt in enumerate(data.get("options") or [], 1):
        lines.append(f"  {i}. {opt}")
    return "\n".join(lines)
