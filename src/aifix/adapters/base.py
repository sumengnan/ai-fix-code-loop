from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Failure:
    """一个失败的测试用例。test_id 必须可直接喂回 run_tests。"""
    test_id: str
    classname: str
    name: str
    message: str
    trace: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class SourceCandidate:
    """嫌疑源码位置，按可疑度排序（越靠前越可疑）。

    origin 是**证据强度**，不是来源标签：

    - `traceback`：失败真的穿过这一帧。最强的证据。
    - `import`：测试文件 import 了它。弱一档 —— 「测试用到了这个模块」不
      等于「缺陷在这个模块」。纯断言失败时栈上根本没有源码帧（被调函数正常
      返回了），这是唯一还拿得到的确定性锚点。

    两者必须分得开。合并成一个列表交给下游，`suspect_anchored` 就答不了
    「这次定位到底是靠什么」，跨 run 也统计不出「退到 import 之后定位准确
    率动没动」—— 而那正是引入这条退路时唯一要回答的问题。
    """
    path: str
    line: int
    frame: str
    origin: str = "traceback"


@dataclass(frozen=True)
class FailureSet:
    failures: dict[str, Failure]
    # 报告里真正跑出结果的用例（通过 + 失败，不含 skipped）。
    # 「不在 failures 里」有三种可能：通过了、被删了、被跳过了。核心循环
    # 只关心第一种，所以一直不需要区分；但挖任务时把后两种当成「红转绿」
    # 会造出无人能通过的假任务，必须能分辨。默认空集 —— 手工构造
    # FailureSet 的既有调用点（verify_node）用不到这个信息。
    ran: frozenset[str] = frozenset()

    @property
    def ids(self) -> set[str]:
        return set(self.failures)


class Verdict(str, Enum):
    BETTER = "better"
    SAME = "same"
    WORSE = "worse"


class ProjectAdapter(Protocol):
    """把「某种语言的测试工程」翻译成核心循环认识的四个问题 + 一个真活。"""

    name: str

    @staticmethod
    def detect(repo: Path) -> bool: ...

    # 命令不接收报告路径：报告写到哪里是构建体系自己的事。Maven surefire
    # 只认 target/surefire-reports/，调用方指定不了。
    def full_test_command(self) -> list[str]: ...

    def scoped_test_command(self, test_ids: list[str]) -> list[str]: ...

    # 返回列表而非单个路径：surefire 每个测试类写一份 TEST-*.xml。
    # scoped 用于区分「全量那一跑」与「复跑那一跑」的报告 —— pytest 侧两者
    # 必须是不同文件，否则复跑会覆盖掉还要继续用的全量报告；Maven 侧同一个
    # 目录，忽略这个参数即可。
    # 一份都没写出来时返回空列表，不抛 —— 「没跑成」由 require_report 那层判定。
    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]: ...

    def test_dirs(self) -> list[str]: ...

    # 挖任务时「哪些后缀算源文件」。判据必须由适配器给，不能写死在挖掘代码
    # 里：`eval/mine.split_paths` 曾经只认 `.py`，于是 Java 仓库的源码全部
    # 落空 → gold_files 恒空 → is_candidate 恒 False → `aifix mine` 对任何
    # Maven 工程产出 0 个任务，且不报错，与「这个仓库最近没有红转绿的提交」
    # 无法区分。
    # 只收产品代码的后缀，不收资源/配置：gold_files 是 locate_hit 的判定
    # 依据，衡量的是 Detector 定位**源文件**的能力，掺进数据文件会稀释它。
    def source_suffixes(self) -> tuple[str, ...]: ...

    # 把「本次 commit 改动过的测试文件路径」翻译成 scoped_test_command 认得的
    # 选择器。这不是恒等映射，只是在 pytest 上恰好长得像恒等映射：pytest 的
    # 选择器就是路径，surefire 的 `-Dtest=` 只认全限定类名。
    # `eval/mine.verify_commit` 曾经写死 `suffix == ".py"` 当作这一步：Maven
    # 任务的 test_files 全是 `.java` → 空 scope → 在 materialize 之前就
    # return []，on_progress 看到的是 n=0，与「这个 commit 没有可用用例」这个
    # 正常结果无法区分。而只把后缀放宽同样不成立 —— 路径原样进 `-Dtest=`，
    # surefire 不报错，安静地一个用例都不跑。
    # 翻不出来的路径（夹具、测试资源、非标准布局）一律丢掉，不猜：猜错的
    # 选择器在两种适配器上都是静默的。
    def test_selectors(self, test_files: list[str]) -> list[str]: ...

    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...

    # 这个 id 指的是一整个测试文件 / 测试类，而不是单个用例吗。
    # 收集阶段整体失败时报告里发的就是这种 id：pytest 的测试文件导入失败发
    # 一条文件级 <error>（id 是文件路径），surefire 的测试类初始化失败发一条
    # name 为空的 <testcase>（id 是裸类名）。挖任务时「测试文件在 C^ 起不来、
    # 在 C 正常」是一整类候选，认不出这种 id 就会把它们静默丢掉。
    # 判据必须由适配器给：`eval/mine` 曾写死 `"::" not in i`，而 `::` 是
    # pytest 的语法 —— Maven 的 id 一个都没有，于是**每一个** Maven id 都被
    # 判成文件级，候选集在复跑那一步被整批清空，verify_commit 返回 [] 且不
    # 报错。
    def is_file_level_id(self, test_id: str) -> bool: ...

    # 一个文件级 id 名下有哪些用例 id。用来判断「这个文件/类在另一侧整体
    # 变绿了」——「至少跑到一个且全都没红」。同样是语法问题：pytest 靠
    # `文件::用例`，surefire 靠 `类#方法`。
    def cases_under(self, file_id: str, test_ids: frozenset[str]) -> set[str]: ...

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
