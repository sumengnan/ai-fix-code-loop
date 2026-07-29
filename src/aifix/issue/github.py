"""往 GitHub 写：状态评论、PR、回执。薄壳，走 runner 自带的 `gh`。

不引 HTTP 客户端也不做重试：Actions 的 runner 上 `gh` 是现成的、已经带着
`GITHUB_TOKEN` 认证好了。自己拼 REST 请求等于把 token 处理、分页、限速全
重写一遍，而这一层的全部工作就是发四种请求。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

# 藏在评论正文里的锚点，用来在下一次 run 时认领自己那条状态评论。
#
# 必须有：没有它就只能「编辑第一条 bot 评论」——而 triage 说明、别的机器人、
# 以及上一次 run 的收尾回帖都长得像候选。认错了就是把别人的话覆盖掉。
STATUS_MARKER = "<!-- aifix:status -->"


def _shell(args: list[str], stdin: str | None = None) -> str:
    res = subprocess.run(args, input=stdin, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"gh 调用失败（退出码 {res.returncode}）：{' '.join(args)}\n"
            f"{res.stderr.strip()}")
    return res.stdout


@dataclass
class GitHubClient:
    """repo 形如 `owner/name`。

    run 是注入口：产品路径用 `_shell`，测试塞一个 recorder 进来。这样命令
    拼得对不对可以脱网断言，而凡是要**解析** GitHub 返回的地方（认领状态
    评论），喂进去的仍是真实响应体。
    """
    repo: str
    run: Callable[..., str] = field(default=_shell)

    # ------------------------------------------------------------ 回执
    def react(self, comment_id: int, emoji: str = "eyes") -> None:
        """给触发命令的那条评论加 reaction。

        比再发一条「收到」轻得多，而且立刻可见 —— Actions 从排队到真正开跑
        有几十秒空窗，这段时间里人需要知道命令有没有被听见。
        """
        self.run(["gh", "api", "--method", "POST",
                  f"repos/{self.repo}/issues/comments/{comment_id}/reactions",
                  "-f", f"content={emoji}"])

    # ------------------------------------------------------------ 状态评论
    def upsert_status(self, issue: int, body: str) -> None:
        """维护**一条**状态评论：有就改，没有就建。

        不刷屏是刻意的：一次 run 要报好几个阶段（收到、复现好了、修完了），
        每段各发一条的话，一个 issue 讨论到一半会被机器人的流水账淹掉。
        """
        marked = f"{body}\n\n{STATUS_MARKER}"
        existing = self._find_status_comment(issue)
        if existing is None:
            self.comment(issue, marked)
            return
        # 走 `--input -`（整个请求体是 JSON）而不是 `-F body=@-`：`-F` 带
        # **类型转换**，正文恰好是 `true` / `123` 这种时会被当成布尔或数值
        # 发出去。这里的正文永远含 STATUS_MARKER，撞不上；但一个只在极少数
        # 输入下变形的编码路径，不值得为省几个字留着。
        self.run(["gh", "api", "--method", "PATCH",
                  f"repos/{self.repo}/issues/comments/{existing}",
                  "--input", "-"],
                 stdin=json.dumps({"body": marked}, ensure_ascii=False))

    def _find_status_comment(self, issue: int) -> int | None:
        raw = self.run(["gh", "api", "--paginate",
                        f"repos/{self.repo}/issues/{issue}/comments"])
        try:
            comments: list[dict[str, Any]] = json.loads(raw or "[]")
        except json.JSONDecodeError:
            # 认不出就当作没有：多发一条评论是噪音，改错一条是破坏。
            return None
        for c in reversed(comments):
            if STATUS_MARKER in (c.get("body") or ""):
                return int(c["id"])
        return None

    # ------------------------------------------------------------ 普通回帖
    def comment(self, issue: int, body: str) -> None:
        """一次性的说明（triage 结论、权限拒绝）。

        **不带 STATUS_MARKER** —— 带上的话，下一次状态更新会认领它并把这段
        说明整个覆盖掉。
        """
        self.run(["gh", "issue", "comment", str(issue), "--repo", self.repo,
                  "--body-file", "-"], stdin=body)

    # ------------------------------------------------------------ PR
    def create_pr(self, head: str, title: str, body: str,
                  base: str | None = None) -> str:
        """开 PR，返回它的 URL。

        **用默认身份（GITHUB_TOKEN，即 github-actions[bot]）开**，不要换成
        仓库主的 PAT：GitHub 不允许批准自己开的 PR，用他自己的身份开出来，
        那个 Approve 按钮对他就是灰的 —— 而 PR review 是 M6 唯一的那道人闸。

        正文走 stdin：报告动辄几千字，还含反引号和换行，塞进 argv 迟早撞上
        长度上限或被 shell 解释。
        """
        args = ["gh", "pr", "create", "--repo", self.repo,
                "--head", head, "--title", title, "--body-file", "-"]
        if base:
            args += ["--base", base]
        return self.run(args, stdin=body).strip()
