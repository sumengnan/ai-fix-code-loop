from __future__ import annotations

from harness.sandbox.base import Sandbox
from harness.tools.base import ToolRegistry
from harness.tools.builtins.fs_tools import ListFilesTool
from harness.types import Message, Role

from ..adapters.base import Failure, ProjectAdapter
from ..signals import under_dirs
from ..tools.edit import EditFileTool
from ..tools.patch import ApplyPatchTool
from ..tools.read import ReadFileTool
from ..tools.read_symbol import ReadSymbolTool
from ..tools.search import GrepTool
from ..tools.tests import RunTestsTool
from .detector import Diagnosis

SYSTEM_PROMPT = """你是一个修复代码缺陷的工程师。工作区是一个 git worktree，你的改动被隔离在这里。

可用工具：
- read_symbol：按名字读一个函数/类的完整定义。**知道要改哪个函数就用它** ——
  不用先 grep 行号、再猜 read_file 的窗口
- read_file / list_files：读整个文件。大文件用 offset 分段读 ——
  截断消息会告诉你下一段从第几行开始，重复读同一段永远拿回同一段
- grep：按正则搜索
- edit_file：把一段原文换成新文本。**这是首选的修改方式**
- apply_patch：应用 unified diff。只在要新建文件、或一次动多个文件时用
- run_tests：跑目标失败用例，验证你的改动

工作方式：
1. 先把要改的那段代码读出来（read_symbol 最省事），确认它**当前**的真实内容。
2. 用 edit_file 改：old_text 从刚读到的内容里**逐字照抄**（去掉行号和那个
   制表符），new_text 写改完的样子。只改必要的几行。
   old_text 必须在文件里唯一 —— 撞车了就多带两行上下文进去。
3. 用 run_tests 验证目标用例是否转绿。
4. 转绿后就停下来给出简短说明；没转绿就根据输出继续调整。

关于 edit_file 与 apply_patch：优先 edit_file。写 diff 要求你数对 `@@` 里的
行数，那件事很容易出错，而出错的补丁一个字节都改不动。edit_file 不需要行号，
也不需要数任何东西。

约束：
- 不能修改测试文件。让测试通过的唯一正确方式是修源码。
- 没有 shell、没有网络、不能装依赖。
- 你必须真的做出修改。只说"已修复"而没有真正改到文件是无效的。"""


def build_registry(sandbox: Sandbox, adapter: ProjectAdapter,
                   known_ids: set[str],
                   touched: set[str] | None = None) -> ToolRegistry:
    """Fixer 的能力面：白名单，七个工具，没有 shell。

    touched：传入一个集合，**每一条写入路径**（apply_patch 与 edit_file）都
    会把成功改动的路径记进去，供交付阶段精确提交（见 Worktree.commit）。
    漏一条 = 改动不进交付分支，而报告照写「已修复」。

    两条写入路径共用 tools.guard.guard_write，围栏上不会因为多一条路而多一
    个洞；越界计数也在 violations._WRITE_TOOLS 里同步列了两条。
    """
    reg = ToolRegistry()
    reg.register(ReadFileTool(sandbox))
    reg.register(ReadSymbolTool(sandbox))
    reg.register(ListFilesTool(sandbox))
    reg.register(GrepTool(sandbox))
    reg.register(EditFileTool(sandbox, test_dirs=adapter.test_dirs(),
                              touched=touched))
    reg.register(ApplyPatchTool(sandbox, test_dirs=adapter.test_dirs(),
                                touched=touched))
    reg.register(RunTestsTool(sandbox, adapter, known_ids=known_ids))
    return reg


# 超过这个数就不列清单：几千行路径塞进开场白既烧钱又淹掉真正有用的那几句，
# 而那种规模下正确的动作本来就是 grep 符号名，不是读目录。
_LISTING_LIMIT = 60

_GREP_ADVICE = ("仓库较大，不列清单。定位请用 grep 搜函数名 / 类名 / "
                "报错里的字符串，不要靠猜路径去 read_file。")


async def locate_hint(sandbox: Sandbox, adapter: ProjectAdapter,
                      diagnosis: Diagnosis | None) -> str | None:
    """诊断指的文件在仓库里不存在时，把真路径（或清单）补给 Fixer。

    纯断言失败的 traceback 里只有测试文件那一帧，detector 只能按包名猜源码
    路径 —— 猜错是常态，`suspect_unanchored` 这条事实记的就是它。实跑里那份
    猜测被原样写进开场白，模型照着去 read_file、报文件不存在，然后一层层
    list_files 试探：一次 run 10 次 list_files、16 次 read_file，其中三次对着
    根本不存在的路径。系统本来就知道这份诊断没有锚点，只是没拿它做任何事。

    只在**猜错时**出声：指得准的时候多一句都是在挤占上下文。
    """
    suspect = (diagnosis.suspect_file or "").strip() if diagnosis else ""
    res = await sandbox.exec(["git", "ls-files"], 30.0)
    tracked = [p.strip() for p in res.stdout.splitlines() if p.strip()]
    if not tracked:
        return None
    if suspect and suspect in tracked:
        return None

    # 测试文件排除在外：改测试是被守卫拦死的动作，列出来只会把模型往那边引
    sources = [p for p in tracked if not under_dirs(p, adapter.test_dirs())]
    if not sources:
        return None

    head = (f"注意：诊断给的嫌疑文件 `{suspect}` 在这个仓库里**不存在**。"
            if suspect else "定位提示：")
    # 文件名对得上是最常见的情形（`cart.py` → `src/shopcart/cart.py`），
    # git 一次 ls-files 就能答，让模型用四五次工具调用去试探是纯粹的浪费
    base = suspect.rsplit("/", 1)[-1]
    same_name = [p for p in sources if p.rsplit("/", 1)[-1] == base] if base else []
    if same_name:
        return head + "仓库里同名的文件是：\n" + "\n".join(f"- {p}" for p in same_name)
    if len(sources) > _LISTING_LIMIT:
        return head + _GREP_ADVICE
    # 说「被 git 跟踪的文件」而不是「源文件」：这份清单是 ls-files 的原样输出，
    # 里面混着 README、锁文件这类东西。把它们说成源文件是一句不必要的假话，
    # 而小仓库里多这几行的代价约等于零。
    return head + "仓库里被 git 跟踪的文件（不含测试）：\n" + "\n".join(
        f"- {p}" for p in sources)


def build_initial_messages(failure: Failure,
                           diagnosis: Diagnosis | None,
                           locate: str | None = None) -> list[Message]:
    """把失败信息与诊断组装成 Fixer 的初始上下文。

    diagnosis 为 None 时降级：直接把原始 traceback 交给 Fixer 自行判断。

    locate：`locate_hint` 的产物，诊断指错文件时补上真路径。两条分支都要带 ——
    降级那条手里只有 traceback，比有诊断时更需要它。
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
    if locate:
        body += "\n\n" + locate
    return [Message(role=Role.USER, content=body)]
