from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol


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
    # 每条 id 是**哪个适配器**跑出来的（适配器名 → 见 nodes.baseline.ADAPTERS）。
    #
    # 前后端同仓的工程一次 run 要跑两套测试，而 detect / fix / 复跑都得知道手上
    # 这条 id 该问谁：`locate_source` 解的栈、`scoped_test_command` 拼的选择器、
    # `is_test_path` 的判据，三个适配器各不相同。
    #
    # **必须记账，不能按 id 形状猜。** 这个仓库为「按形状猜」栽过一次：
    # `eval/mine` 写死 `"::" not in i` 判文件级 id，而 `::` 是 pytest 的语法，
    # Maven 的 id 一个都没有 —— 于是**每一个** Maven id 都被判成文件级，候选集
    # 在复跑那一步被整批清空，且不报错。vitest 的 id 又恰好也用 `::`，再猜一次
    # 只会把两者混在一起。
    #
    # 默认空 dict，与 `ran` 同一条理由：手工构造 FailureSet 的既有调用点
    # （verify_node 里那两处）用不到这个信息。单适配器时全项目也不会去读它。
    owner: dict[str, str] = field(default_factory=dict)

    @property
    def ids(self) -> set[str]:
        return set(self.failures)


def merge(sets: "Iterable[FailureSet]") -> FailureSet:
    """把多个适配器各自跑出来的结果并成一个。

    并集，不做去重判断：不同适配器的 id 天然不撞（pytest 是 `.py` 文件路径、
    vitest 是 `.ts`/`.tsx`、Maven 是全限定类名）。真撞上了后来者覆盖前者——
    那说明注册表配错了，而**静默合并**比静默丢弃好排查：报告里会同时出现两条
    长得一样的 id，一眼看得出不对。

    合并之后 `verify.compare` 一行都不用改 —— 它是纯集合运算，不认识适配器。
    这正是「三态判定不该知道语言」那条设计在多适配器上兑现的地方。
    """
    failures: dict[str, Failure] = {}
    ran: set[str] = set()
    owner: dict[str, str] = {}
    for fs in sets:
        failures.update(fs.failures)
        ran |= set(fs.ran)
        owner.update(fs.owner)
    return FailureSet(failures, ran=frozenset(ran), owner=owner)


def tag_owner(fs: FailureSet, name: str) -> FailureSet:
    """给一份刚跑出来的结果盖上「这是谁跑的」。

    盖的是 `ran` 而不是 `failures`：**通过的用例也要记账**。复跑一条当前没红的
    用例（flaky 确认就在做这件事）同样要知道该问哪个适配器，而它不在 failures
    里。只记失败的话，那条路会在「这条 id 没有 owner」上退化成默认适配器 ——
    默认对了是巧合，错了是静默跑空。
    """
    return FailureSet(fs.failures, ran=fs.ran,
                      owner={i: name for i in set(fs.ran) | set(fs.failures)})


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

    # 测试**住在**哪。只用于两件事：给 reproducer 的提示词说明「新测试写哪去」，
    # 以及挖任务时把改动路径拆成测试侧/源文件。
    #
    # **判断「这个文件是不是测试」用 `is_test_path`，不要用这个。** 见下。
    def test_dirs(self) -> list[str]: ...

    # 这个路径是不是测试文件。**「不许改测试文件」那道守卫的唯一判据。**
    #
    # 为什么不能继续用 `under_dirs(path, test_dirs)`：那等于断言「测试都住在
    # 某个目录下」，而这个前提对 vitest 不成立 —— JS 生态的主流约定是测试与源码
    # **同目录**（`src/components/ChatView.test.tsx`），靠后缀而不是目录区分。
    # 拿目录列表去表达它，两条路都是坏的：返回 `[]` 会让守卫**静默放行**，于是
    # 修复阶段的 agent 可以直接改掉自己的判卷标准；返回 `["src"]` 又会把整个源码
    # 树判成测试，什么都改不了。
    #
    # 所以判据必须由适配器给，而且必须是**谓词**而不是目录列表。pytest 与 Maven
    # 的实现就是 `under_dirs(path, self.test_dirs)`，行为与改造前逐字节相同。
    #
    # 传的是**谓词这个值**，不是 adapter 对象 —— 与 `eval/mine.split_paths` 那段
    # 「两个判据都是传进来的值」是同一条理由：拿着 adapter 就得在测试里造一个假
    # 适配器才能覆盖一种布局。
    #
    # 这道守卫**已经静默失效过一次**（见 `signals.under_dirs` 的注释：Maven 的
    # `["src/test"]` 遇上当时只比首段的实现，首段是 `src`，直接放行）。它失效时
    # 不报错、报告照样显示绿，只是绿的理由变成了「模型把测试改了」。
    def is_test_path(self, path: str) -> bool: ...

    # 挖任务时「哪些后缀算源文件」。判据必须由适配器给，不能写死在挖掘代码
    # 里：`eval/mine.split_paths` 曾经只认 `.py`，于是 Java 仓库的源码全部
    # 落空 → gold_files 恒空 → is_candidate 恒 False → `aifix mine` 对任何
    # Maven 工程产出 0 个任务，且不报错，与「这个仓库最近没有红转绿的提交」
    # 无法区分。
    # 只收产品代码的后缀，不收资源/配置：gold_files 是 locate_hit 的判定
    # 依据，衡量的是 Detector 定位**源文件**的能力，掺进数据文件会稀释它。
    def source_suffixes(self) -> tuple[str, ...]: ...

    # 把「本次 commit 改动过的测试文件路径」翻译成 scoped_test_command 认得的
    # 选择器。**这不是恒等映射**，只是在 pytest 上恰好长得像：pytest 的选择器
    # 就是路径，surefire 的 `-Dtest=` 只认全限定类名。按后缀写死会让 Maven 任务
    # 得到空 scope，而那与「这个 commit 没有可用用例」区分不开；只放宽后缀同样
    # 不成立 —— 路径原样进 `-Dtest=`，surefire 不报错，安静地一个用例都不跑。
    # 翻不出来的路径（夹具、测试资源、非标准布局）一律丢掉，不猜。
    def test_selectors(self, test_files: list[str]) -> list[str]: ...

    def make_test_id(self, classname: str, name: str, file: str | None) -> str: ...

    # 这套体系的用例 id 长什么样 —— **给模型看的样例**，不是给程序解析的。
    # 只说「格式与本项目其余用例一致」对没见过本项目 id 的模型等于没说：它会
    # 写出正确的测试却把 id 写成 unittest 方言，被「id 要能追溯到 test_file」
    # 那道闸打回，整轮作废。
    # 各家语法差得很远（`文件::用例` / `类#方法` / `文件::描述 > 用例`），
    # 所以只能由适配器给。
    def example_test_id(self) -> str: ...

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

    # 跑测试之前，在 worktree 里补上 git 带不过去的东西。
    #
    # worktree 只含**被跟踪**的文件，而各语言的依赖目录都不被跟踪：
    # `node_modules` / `.venv` / `~/.m2`。pytest 与 Maven 各有现成的解法
    # （前者用 worktree 之外的绝对路径解释器，后者用本机仓库），vitest 没有 ——
    # 它按 cwd 向上找 `node_modules`，而 worktree 里那条路上一个都没有。
    #
    # 必须**幂等且便宜**：`run_full_suite` 与 `run_scoped` 各调一次，而一次
    # run 里它们要跑好几轮。
    def prepare(self, worktree: Path) -> None: ...

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]: ...
