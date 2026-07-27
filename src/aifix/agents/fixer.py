from __future__ import annotations

from harness.sandbox.base import Sandbox
from harness.tools.base import ToolRegistry
from harness.tools.builtins.fs_tools import ListFilesTool, ReadFileTool
from harness.types import Message, Role

from ..adapters.base import Failure, ProjectAdapter
from ..tools.patch import ApplyPatchTool
from ..tools.search import GrepTool
from ..tools.tests import RunTestsTool
from .detector import Diagnosis

SYSTEM_PROMPT = """你是一个修复代码缺陷的工程师。工作区是一个 git worktree，你的改动被隔离在这里。

可用工具：
- read_file / list_files：查看代码
- grep：按正则搜索
- apply_patch：应用 unified diff（唯一的修改手段）
- run_tests：跑目标失败用例，验证你的改动

工作方式：
1. 先 read_file 确认你要改的文件当前的真实内容——不要凭记忆写 diff。
2. 用 apply_patch 提交最小的改动。只改必要的行，不要重写整个文件。
3. 用 run_tests 验证目标用例是否转绿。
4. 转绿后就停下来给出简短说明；没转绿就根据输出继续调整。

约束：
- 不能修改测试文件。让测试通过的唯一正确方式是修源码。
- 没有 shell、没有网络、不能装依赖。
- 你必须真的做出修改。只说"已修复"而没有调用 apply_patch 是无效的。"""


def build_registry(sandbox: Sandbox, adapter: ProjectAdapter,
                   known_ids: set[str]) -> ToolRegistry:
    """Fixer 的能力面：白名单，五个工具，没有 shell。"""
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    reg.register(ApplyPatchTool(sandbox, test_dirs=adapter.test_dirs()))
    reg.register(RunTestsTool(sandbox, adapter, known_ids=known_ids))
    return reg


def build_initial_messages(failure: Failure,
                           diagnosis: Diagnosis | None) -> list[Message]:
    """把失败信息与诊断组装成 Fixer 的初始上下文。

    diagnosis 为 None 时降级：直接把原始 traceback 交给 Fixer 自行判断。
    """
    if diagnosis is None:
        body = (
            f"请修复这个失败的测试：{failure.test_id}\n\n"
            f"断言信息：{failure.message}\n\n"
            f"完整 traceback：\n{failure.trace}\n\n"
            "（自动定位未能给出可用诊断，请自行从 traceback 判断缺陷位置。）")
    else:
        lines = (f"{diagnosis.suspect_lines[0]}-{diagnosis.suspect_lines[1]}"
                 if diagnosis.suspect_lines else "未知")
        body = (
            f"请修复这个失败的测试：{failure.test_id}\n\n"
            f"断言信息：{failure.message}\n\n"
            f"定位分析（置信度 {diagnosis.confidence}）：\n"
            f"  嫌疑文件：{diagnosis.suspect_file}\n"
            f"  嫌疑行号：{lines}\n"
            f"  根本原因：{diagnosis.root_cause}\n"
            f"  修复思路：{diagnosis.fix_strategy}\n\n"
            "这份分析仅供参考，请自己读代码确认后再动手。")
    return [Message(role=Role.USER, content=body)]
