"""解析 GitHub 的事件载荷，判定这一条要不要处理。零 LLM。

两条入口：

- `issues` / `opened` —— **主入口**，正文第一行是 `/aifix` 的新 issue。
  这条路上触发的动作与被读的文本是同一个人写的同一个对象，注入面**结构上**归零。
- `issue_comment` / `created` —— 用来再跑一次，以及回答 `/aifix <编号>`。
  这条路要查两个人：按按钮的（评论者）和写正文的（issue 作者）。

判定权不能交给模型：让它读一段文本去回答「这算不算授权」，等于把闸门交给一个
可以被说服的东西。这里只有字符串比较和字典取值。
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


def _strip_command(body: str) -> str:
    """去掉正文开头那行 `/aifix`，剩下的就是缺陷描述。

    只去第一行、且只在它确实是命令时去 —— 正文里别处出现的 `/aifix`（比如
    在讲怎么用它）是内容，不是标记。
    """
    text = body.replace("\r\n", "\n")
    head, sep, rest = text.partition("\n")
    if head.strip() != COMMAND:
        return text.strip()
    return rest.strip() if sep else ""


def authorize(payload: dict[str, Any],
              allowed_users: frozenset[str] = frozenset()) -> Decision:
    """这条事件要不要触发一次修复。两条入口，判定都在这里。

    - **issue 正文**以 `/aifix` 开头（`issues` / `opened`）—— 开 issue 即触发
    - **评论**第一行是 `/aifix` 或 `/aifix <编号>`（`issue_comment` / `created`）

    `allowed_users` 是显式白名单（**登录名，已 casefold**）。它是**参数不是环境
    变量**：这个函数是全项目最要紧的一道判定，保持纯函数才能被脱网穷举 ——
    读环境会让它的行为取决于测试跑在谁的机器上。调用方从 config 取值传进来。
    """
    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}

    if "pull_request" in issue:
        # issue_comment 对 PR 也会触发 —— GitHub 眼里 PR 就是一种 issue。
        # 不排除的话，任何人在 PR 下随口一句都可能撞上命令前缀。
        return Decision(False, "这是 PR 不是 issue")

    if "comment" in payload:
        return _from_comment(payload, issue, repo, allowed_users)
    return _from_issue(payload, issue, repo, allowed_users)


# GitHub 的 author_association 里，**有 write 权限**的那几个。
#
# 判据是「触发权 = 已经能改这个仓库的人」：issue 正文会驱动模型改代码、开 PR，
# 而一个本来就能直接推代码的人驱动它，不增加任何新风险；反过来，一个没有 write
# 权限的人能驱动它，等于给了他一条间接的写路径（PR 的内容由他的文字决定）。
#
# CONTRIBUTOR **不在此列**，这是最容易写错的一条：它的含义只是「有 commit 进过
# 这个仓库」—— 一年前合过一个改错别字的 PR，就永久是 CONTRIBUTOR，而他今天对
# 这个仓库没有任何权限。
_WRITE_ASSOCIATIONS = frozenset({"OWNER", "COLLABORATOR"})

# MEMBER = 组织成员，只在**组织仓库**上认。
#
# 两点要写明：个人仓库上 GitHub 不发这个值，真收到了说明载荷不是我们理解的
# 那样，此时不放行（守卫宁可多拦不可漏放）；而即便在组织仓库上，MEMBER 也
# **不保证**对这一个仓库有 write —— 它比上面那条线略宽。要精确到 write 得调
# `/repos/{o}/{r}/collaborators/{u}/permission`，那是一次网络调用，会让这个
# 纯函数变成有 IO 的东西。需要更严就把组织仓库的人加成 COLLABORATOR，或者
# 反过来用白名单点名。
_ORG_ASSOCIATION = "MEMBER"


def _is_trusted(assoc: str, login: str, allowed_users: frozenset[str],
                is_org: bool, owner_login: str = "") -> bool:
    """这个人能不能驱动 aifix。零 IO，只看载荷里已有的字段 + 白名单。

    第一条判据是**登录名就等于仓库账号本身**，它比 `author_association` 更确定：
    个人仓库的主人对自己的仓库永远有全部权限，这一条不依赖 GitHub 那套关系
    分类。留着它是防御性的 —— 万一某种事件形状里 `author_association` 缺失或
    变了取值，仓库主不至于被自己的工具锁在门外。
    它不会放宽任何人：GitHub 的用户名与组织名共用一个命名空间，一个 login 要么
    是人要么是组织，不可能两者都是；所以组织仓库上这一条恒不成立。
    """
    if login and owner_login and login.casefold() == owner_login.casefold():
        return True
    if login and login.casefold() in allowed_users:
        return True
    if assoc in _WRITE_ASSOCIATIONS:
        return True
    return assoc == _ORG_ASSOCIATION and is_org


def _is_org(repo: dict[str, Any]) -> bool:
    return ((repo.get("owner") or {}).get("type") == "Organization")


def _refuse_actor(login: str) -> Decision:
    """看起来是命令但没权限。**必须回帖** —— 静默丢弃会让人以为它已经在跑了。"""
    return Decision(
        False,
        f"@{login} 没有触发 aifix 的权限。\n"
        "  这个命令对仓库所有者与有写入权限的协作者开放"
        "（组织仓库还包括组织成员）。\n"
        "  需要开权限的话，请仓库管理者把你加为协作者，"
        "或把你的登录名加进 `AIFIX_ALLOWED_USERS`。",
        notify=True)


def _from_issue(payload: dict[str, Any], issue: dict[str, Any],
                repo: dict[str, Any],
                allowed_users: frozenset[str]) -> Decision:
    """issue 正文触发。

    这条路上**触发的动作与被读的文本是同一个人写的同一个对象** —— 一条判据
    同时管住「谁按的按钮」和「谁写的文本」，注入面是结构上归零的，不靠两条
    判据凑。
    """
    if payload.get("action") != "opened":
        # 只认 opened。改自己的 issue 正文是**完全静默**的：没有新通知、时间线
        # 上只有一个不起眼的 edited 标记。接了 edited，一条半年前的 issue 被改成
        # /aifix 开头就能触发，而没有任何人会知道。
        return Decision(False, "不是新开的 issue")

    if (issue.get("user") or {}).get("type") == "Bot":
        return Decision(False, "issue 来自 bot")

    body = issue.get("body") or ""
    if _first_line(body) != COMMAND:
        # 绝大多数 issue 都不是命令，这一类一个字都不能回。
        return Decision(False, f"issue 正文第一行不是 {COMMAND}")

    author = ((issue.get("user") or {}).get("login") or "")
    owner = ((repo.get("owner") or {}).get("login") or "")
    if not _is_trusted(issue.get("author_association") or "", author,
                       allowed_users, _is_org(repo), owner):
        return _refuse_actor(author)

    return Decision(True, event=IssueEvent(
        number=int(issue.get("number", 0)),
        title=issue.get("title") or "",
        # **去掉那行标记再交给模型**：`/aifix` 是给机器看的，不是缺陷描述的
        # 一部分。原样喂进去的话，模型上下文的第一句是一个它不认识的命令词。
        body=_strip_command(body),
        repo_full_name=repo.get("full_name") or "",
        owner=owner,
        commenter=author,
        # issue 正文触发没有「那条评论」可以回执，reaction 打在 issue 上要用
        # 另一个 API。0 表示没有可加 reaction 的评论（见 handle）。
        comment_id=0,
    ))


def _from_comment(payload: dict[str, Any], issue: dict[str, Any],
                  repo: dict[str, Any],
                  allowed_users: frozenset[str]) -> Decision:
    """评论触发。用来**再跑一次**，以及回答 `/aifix <编号>`。

    判据比 issue 那条**多一项**，因为这条路上两者不是同一个人：模型读的是
    issue 正文，而按按钮的是评论者。所以写正文的人和按按钮的人都要可信。
    """
    if payload.get("action") != "created":
        # 只认 created，理由同 _from_issue 里那段。
        return Decision(False, "不是新建的评论")

    comment = payload.get("comment") or {}
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
    is_org = _is_org(repo)
    owner = ((repo.get("owner") or {}).get("login") or "")
    commenter = ((comment.get("user") or {}).get("login") or "")
    if not _is_trusted(comment.get("author_association") or "", commenter,
                       allowed_users, is_org, owner):
        return _refuse_actor(commenter)

    author = ((issue.get("user") or {}).get("login") or "")
    if not _is_trusted(issue.get("author_association") or "", author,
                       allowed_users, is_org, owner):
        # **只限制触发者挡不住提示注入。** 攻击路径是「外人提一个藏了指令的
        # issue，等有权限的人觉得该修、顺手打上 /aifix」—— 而那个人本来就想修
        # bug，那一步门槛低得可怜。模型读到的每个字都得出自可信的人。
        return Decision(
            False,
            f"这个 issue 是 @{author} 提的，而他没有触发 aifix 的权限 —— "
            "所以它的正文不能直接交给模型。\n"
            "  这不是权限洁癖：issue 正文会作为输入喂给模型，"
            "而不可信来源的正文可能藏着指令。\n"
            f"  要修它的话，请自己新开一个 issue、正文第一行写 `{COMMAND}`，"
            "把缺陷用你自己的话复述一遍。",
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
