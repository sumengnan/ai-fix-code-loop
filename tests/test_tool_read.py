"""`read_file` 必须能读到大文件的**后半部分**。

这条是实测逼出来的（2026-07-30，issue #2 的真跑）：模型第一次调用就直奔正确
的文件 `src/aifix/cli.py`（46,447 字符），grep 到了目标函数在第 519 行 ——
然后把同一个文件**读了五遍**，每遍都拿回一模一样的前 8000 字符，最后额度耗尽、
零产出。

框架的 ReadFileTool 只有 `path` 一个参数，截断消息是「…(已截断)」——它告诉
模型内容被切了，却**不给任何拿到剩下部分的办法**。于是重读是模型唯一能想到的
动作，而重读永远拿回同一段。

这不只是 M6 的问题：fixer 用的是同一个工具，仓库里 48 个源文件有 18 个超过
8000 字符，它们 200 行之后的缺陷对整个系统都够不着。
"""
import pytest
from harness.sandbox.local import LocalSandbox

from aifix.tools.read import ReadFileTool


@pytest.fixture
def big_file(tmp_path):
    lines = [f"line {i}: " + "x" * 60 for i in range(1, 501)]
    lines[449] = "def the_target_function():  # 第 450 行"
    (tmp_path / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


async def _read(root, **kw):
    t = ReadFileTool(LocalSandbox(workspace=str(root)), max_chars=1200)
    return await t.run(t.Params(**kw))


async def test_the_tail_of_a_big_file_is_reachable(big_file):
    """核心诉求：第 450 行读得到。

    没有 offset 的话，1200 字符只够看到前十几行 —— 那个函数永远在截断线之后。
    """
    out = await _read(big_file, path="big.py", offset=445, limit=10)
    assert "the_target_function" in out


async def test_truncation_says_how_to_continue(big_file):
    """截断消息必须**可操作**。

    「…(已截断)」只说明发生了截断，不说下一步 —— 模型于是重读，而重读拿回
    同一段。消息里要带下一个 offset，这一条正是那次真跑的直接教训。
    """
    out = await _read(big_file, path="big.py")
    assert "offset" in out, out[-200:]
    # 反向对照：文件没被截断时不该凭空出现续读提示
    (big_file / "small.py").write_text("x = 1\n", encoding="utf-8")
    assert "offset" not in await _read(big_file, path="small.py")


async def test_the_hint_offset_actually_lands_on_the_next_unread_line(big_file):
    """提示里的 offset 得是**真能接上**的那一行，不能差一行。

    差一行的后果是静默的：模型照着提示续读，中间少一行代码，而它不会知道。
    """
    first = await _read(big_file, path="big.py", offset=1)
    hint = int(first.rsplit("offset=", 1)[1].split()[0].rstrip("）)。 ,"))
    # 第一段最后一行的下一行，就该是续读那一段的第一行
    # 行号与内容之间是制表符（与 Read 工具同形，便于模型照抄上下文）
    last_line_no = max(int(ln.split("\t", 1)[0].strip())
                       for ln in first.splitlines()
                       if "\t" in ln and ln.split("\t", 1)[0].strip().isdigit())
    assert hint == last_line_no + 1, f"提示 offset={hint}，实际读到第 {last_line_no} 行"


async def test_lines_are_numbered(big_file):
    """带行号：模型要靠它写 apply_patch 的上下文，也要靠它判断续读位置。"""
    out = await _read(big_file, path="big.py", offset=100, limit=3)
    assert "100" in out and "102" in out


async def test_reading_past_the_end_says_so_instead_of_returning_nothing(big_file):
    """越过文件末尾返回空串的话，模型分不出「这个文件到头了」和「读失败了」。"""
    out = await _read(big_file, path="big.py", offset=9999)
    assert "500" in out, out          # 总行数要说出来
    assert out.strip()


async def test_path_escapes_are_refused(big_file):
    """与 resolve_in_workspace 同一条底线 —— 这个工具是白名单能力面的一员。"""
    with pytest.raises(Exception):
        await _read(big_file, path="../etc/passwd")


async def test_both_agents_get_the_ranged_reader(tmp_path):
    """fixer 与 reproducer 必须用**同一个**能分段读的工具。

    漏掉任一个，那一侧就还停在「大文件读不到尾」的老状态 —— 而这种失效不报错，
    只表现为模型反复读同一个文件然后耗尽额度。
    """
    from aifix.adapters.pytest_adapter import PytestAdapter
    from aifix.agents.fixer import build_registry
    from aifix.reproduce import build_reproduce_registry
    from aifix.tools.read import ReadFileTool as Ranged

    sb = LocalSandbox(workspace=str(tmp_path))
    for reg in (build_registry(sb, PytestAdapter(), known_ids=set()),
                build_reproduce_registry(sb, PytestAdapter())):
        tool = reg.get("read_file")
        assert isinstance(tool, Ranged), type(tool)
        assert "offset" in tool.Params.model_fields
