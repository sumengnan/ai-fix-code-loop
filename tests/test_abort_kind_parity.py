"""两条入口对「这次 run 没跑成」的判据必须一致。

`aifix run` 的退出码由 `cli._FAILED_RUN_KINDS` 判，issue 那条由
`issue.handle._ENV_ABORTS` 判。**两份名单，同一个语义** —— 而两份各写各的
东西，在这个仓库里从来只有一个结局：加了一种中止只改一处。

这不是假想。2026-08-01 的功能巡检就撞上了它的前身：`preflight_node` 根本
不写 `abort_kind`，于是

    $ aifix run /打错的/路径 ; echo $?
    **中止**：没有适配器认领这个项目：/打错的/路径
    0                                    ← 流水线读成成功

同一个洞在 issue 那条路上表现为：Actions 的 job **绿着结束**，而这次 run
一个用例都没跑过。

两份名单**故意不共用同一个常量**（理由写在 handle.py 那段注释里：两条入口
的判据将来可以有意分叉）。既然不共用，就得有东西把它们钉在一起 —— 就是
这个文件。
"""
import pytest

from aifix.cli import _FAILED_RUN_KINDS
from aifix.graph import (COLLECTION_ABORT_KIND, MODEL_ABORT_KIND,
                         PREFLIGHT_ABORT_KIND)
from aifix.issue.handle import _ENV_ABORTS


def test_the_two_lists_agree():
    """要分叉可以，但必须是**有意**的：分叉时这条测试会红，改它的人得先
    在这里写清楚为什么。悄悄漂移则不行。"""
    assert set(_FAILED_RUN_KINDS) == set(_ENV_ABORTS), (
        f"命令行与 issue 两条入口的判据不一致：\n"
        f"  只在 cli：    {set(_FAILED_RUN_KINDS) - set(_ENV_ABORTS)}\n"
        f"  只在 handle： {set(_ENV_ABORTS) - set(_FAILED_RUN_KINDS)}")


@pytest.mark.parametrize("kind", [
    "crash", COLLECTION_ABORT_KIND, MODEL_ABORT_KIND, PREFLIGHT_ABORT_KIND])
def test_every_known_failure_kind_is_covered(kind):
    """把四种逐个点名。

    只断言「两边相等」是不够的 —— 两边**同时**漏掉一种时它照样绿，而那正是
    preflight 那次的真实形态：两处都没有它。
    """
    assert kind in _FAILED_RUN_KINDS
    assert kind in _ENV_ABORTS


def test_running_out_of_budget_is_not_a_failed_run():
    """反向对照，也是这条判据的**全部意义**所在。

    预算耗尽是**正常收场**：活干到钱花完为止，已经修好的那些仍然可信、仍然
    躺在交付分支上。把它算成失败，等于让每一次「钱花完了」都在流水线里报警，
    而那会让人很快把整个退出码判据关掉。
    """
    for kind in ("budget_tokens", "budget_usd", "budget_wall", "needs_input"):
        assert kind not in _FAILED_RUN_KINDS, kind
        assert kind not in _ENV_ABORTS, kind
