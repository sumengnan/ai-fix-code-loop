"""解析 GitHub 的 `issue_comment` 载荷，判定这一条要不要处理。零 LLM。

判定权不能交给模型：让它读一句评论去回答「这算不算授权」，等于把闸门交给
一个可以被说服的东西。这里只有字符串比较和字典取值。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMAND = "/aifix"

# `/aifix 2` —— 回答上一轮 `ask_user` 提的那个问题。
#
# 为什么是编号而不是自由文本：自由回复要再过一次模型去解析意图，而那一步
# 出错的方式是**按用户没说过的意图改了代码** —— 比不问更糟。编号让「人说了
# 什么」到「机器做什么」这一段是纯确定性的。
_ANSWER = re.compile(rf"^{re.escape(COMMAND)}\s+(\d+)$")


@dataclass(frozen=True)
class IssueEvent:
    number: int
    title: str
    body: str
    repo_full_name: str
    owner: str
    commenter: str
    comment_id: int
    # `/aifix 2` 里那个 2（从 1 数起），普通的 `/aifix` 是 None。
    # 权限判定对两者**完全一样** —— 回答一个问题会直接决定代码怎么改，
    # 它和发起一次修复是同一级别的动作，不该有一条更宽的门。
    answer_choice: int | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    # 拒绝要不要回帖。两种拒绝的性质完全不同：
    #
    # - **没人在等** —— 这条评论压根不是命令（绝大多数评论都不是）、是 PR 上
    #   的、是 bot 发的。回帖等于每条闲聊都被机器人怼一句「这不是命令」，比不
    #   回还糟。
    # - **有人在等** —— 看起来是命令，但权限不够。这一类必须出声：静默丢弃会
    #   让人以为它已经在跑了，而「不报错、只有承诺是假的」正是本项目栽过十次
    #   以上的那种失败。
    notify: bool = False
    event: IssueEvent | None = None


def load_payload(path: str | Path) -> dict[str, Any]:
    """读 Actions 写在 `$GITHUB_EVENT_PATH` 的事件载荷。

    读不到时抛一句**指得到方向**的错，而不是让 FileNotFoundError 裸穿：
    本地调试忘了设这个变量是最常见的第一次失败，而裸异常只会说某个临时
    路径不存在，没有一个字提示它本该由 Actions 提供。
    """
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(
            f"读不到事件载荷：{p}\n"
            "  这个文件由 GitHub Actions 写出，路径在环境变量 GITHUB_EVENT_PATH 里。\n"
            "  本地调试时可以自己造一份（tests/fixtures/ 下有一个真实形状的样例），"
            "然后 export GITHUB_EVENT_PATH=/path/to/event.json。") from e


def _first_line(body: str) -> str:
    """取第一行并去掉行尾空白。

    `\\r` 必须去掉：GitHub 的评论正文用 CRLF，按 `\\n` 切完第一行是
    `"/aifix\\r"`，与 `"/aifix"` 不相等 —— 命令永远匹配不上，而且一声不吭。

    只看第一行、不全文搜索：正文里引用别人的话、或贴一段命令示例，都会被
    全文搜索当成指令。
    """
    return body.replace("\r\n", "\n").split("\n", 1)[0].strip()


def authorize(payload: dict[str, Any]) -> Decision:
    """这一条评论要不要触发一次修复。

    判据全部同时成立才放行。顺序是刻意的：先把「没人在等」的几类静静滤掉，
    再判权限——反过来的话，仓库里每一条普通评论都会收到一句权限说明。
    """
    if payload.get("action") != "created":
        # 只认 created。接 edited 的话，一条三个月前的旧评论被编辑成 /aifix
        # 就能触发，而编辑不留下新的通知，没人会注意到。
        return Decision(False, "不是新建的评论")

    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    repo = payload.get("repository") or {}

    if "pull_request" in issue:
        # issue_comment 对 PR 也会触发 —— GitHub 眼里 PR 就是一种 issue。
        # 不排除的话，任何人在 PR 下随口一句都可能撞上命令前缀。
        return Decision(False, "这是 PR 不是 issue")

    if (comment.get("user") or {}).get("type") == "Bot":
        # 自己回的帖不能把自己再唤醒一次。Actions 里用 GITHUB_TOKEN 发的评论
        # 不会再触发 workflow（GitHub 内建的防递归），但 repository_dispatch
        # 那条路永远会触发，不受那层保护 —— 这一道是给那条路留的。
        return Decision(False, "评论来自 bot")

    first = _first_line(comment.get("body") or "")
    choice: int | None = None
    if first != COMMAND:
        m = _ANSWER.match(first)
        if m is None:
            return Decision(False, f"第一行不是 {COMMAND}")
        choice = int(m.group(1))

    # 走到这里，有人确实在等一个回应了 —— 下面的拒绝全部 notify=True。
    owner = ((repo.get("owner") or {}).get("login") or "")
    commenter = ((comment.get("user") or {}).get("login") or "")

    if comment.get("author_association") != "OWNER":
        # 只认 OWNER。CONTRIBUTOR 尤其不是信任信号 —— 它的含义只是「有 commit
        # 进过这个仓库」，一年前合过一个改错别字的 PR 就永久是 CONTRIBUTOR。
        return Decision(
            False,
            f"@{commenter} 没有触发 aifix 的权限：这个命令只对仓库所有者开放。",
            notify=True)

    if (issue.get("user") or {}).get("login") != owner:
        # 只限制触发者挡不住提示注入。攻击路径是「外人提一个藏了指令的 issue，
        # 等仓库主觉得该修、顺手打上 /aifix」—— 而仓库主本来就想修 bug，那一步
        # 门槛低得可怜。模型读到的每个字都得是仓库主自己写的，注入面才归零。
        return Decision(
            False,
            "aifix 目前只处理仓库所有者**自己提出**的 issue。\n"
            "  这条限制不是权限洁癖：issue 正文会作为输入交给模型，"
            "而外部提交的正文是不可信文本。\n"
            "  要修这个问题的话，请另开一个 issue 把它复述一遍。",
            notify=True)

    return Decision(True, event=IssueEvent(
        number=int(issue.get("number", 0)),
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        repo_full_name=repo.get("full_name") or "",
        owner=owner,
        commenter=commenter,
        comment_id=int(comment.get("id", 0)),
        answer_choice=choice,
    ))
