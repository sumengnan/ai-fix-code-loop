"""写入前的三道检查，**所有**能改文件的工具共用这一份。

抽出来是因为 `edit_file` 的出现：在那之前 `apply_patch` 是唯一的写入路径，
守卫写在它自己身上没问题；多一条写入路径而守卫各写各的，迟早会有一条漏掉
其中一项 —— 而漏掉的后果是静默的：报告仍然显示绿，只是绿的理由变成了「模型
把测试改了」。

三道，顺序无所谓，都是硬拒绝：
1. `.git` 目录 —— 改它等于篡改历史与交付的依据；
2. 测试目录 —— 让测试通过的唯一正确方式是改源码，这是整个项目的地基；
3. 工作区围栏 —— 逃逸出去改的就不是这次 run 的东西了。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from harness.sandbox.base import Sandbox, SandboxError, resolve_in_workspace
from harness.tools.base import ToolError

from ..signals import under_dirs


def guard_write(sandbox: Sandbox, test_dirs: list[str], real: str,
                raw: str | None = None) -> None:
    """`real` 是**实际写入**的路径；`raw` 是调用方给的原样写法（可选）。

    两条都查测试目录：守卫宁可多拦不可漏放。diff 那边 `--- a/tests/x.py`
    剥完前缀是 `tests/x.py`，而 `diff.noprefix` 风格的 `--- tests/x.py` 剥完
    反而不像测试文件了 —— 它本来就是。围栏与 `.git` 只按写入路径判，因为
    它们问的是「最终会落到哪」。
    """
    if ".git" in PurePosixPath(real).parts:
        raise ToolError(f"拒绝修改 .git 目录下的文件：{real}")
    dirs = [d.strip("/") for d in test_dirs]
    if under_dirs(real, dirs) or (raw is not None and under_dirs(raw, dirs)):
        raise ToolError(
            f"拒绝修改测试文件：{real}。"
            "请修改源码使测试通过，而不是修改测试本身。")
    try:
        resolve_in_workspace(sandbox.workspace, real)
    except SandboxError as e:
        raise ToolError(str(e)) from None
