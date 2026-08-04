"""vitest 适配器。JS/TS 前端工程的测试面。

与另外两个适配器最大的不同：**测试不住在单独的目录里**。JS 生态的主流约定是
测试与源码同放（`src/components/ChatView.test.tsx`），所以「这是不是测试文件」
只能按后缀判 —— `ProjectAdapter.is_test_path` 这个谓词就是为它加的。

下面每一条判断都来自实跑（2026-08-03，vitest 2.1.9 / node 24），不是照文档推的。
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .base import Failure, SourceCandidate

# vitest 的栈帧行：` ❯ inner src/lib.ts:2:20`，函数名可缺（` ❯ src/a.test.ts:4:19`）。
#
# 实跑样本：
#     ❯ inner src/probe/lib.ts:2:20          ← 最深，抛出点
#     ❯ Module.outer src/probe/lib.ts:6:10
#     ❯ src/probe/p.test.ts:5:12             ← 入口，无函数名
#
# **由深到浅**，和 Java 一样、和 Python 相反 —— 所以不像 PytestAdapter 那样 reverse。
#
# 行尾锚定到 `:行:列`：路径里可以有空格（`src/my dir/a.ts`），而函数名与路径之间
# 也是空格，只按空格切会在两种情况下各切错一次。锚定行尾之后，路径就是「最后一个
# `:数字:数字` 之前的全部内容」，两种情况都对。
_FRAME = re.compile(
    r"^\s*❯\s+(?:(?P<fn>\S+)\s+)?(?P<path>.+?):(?P<line>\d+):\d+\s*$",
    re.MULTILINE)

# 测试文件后缀。`__tests__/` 那种目录布局也常见，但它同样满足这套后缀约定
# （目录里的文件仍叫 `x.test.ts`），所以不用为它单开一条判据。
_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                  ".test.mts", ".test.cts",
                  ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
                  ".spec.mts", ".spec.cts")

# 产品代码后缀。`.d.ts` 刻意不收：类型声明里没有可执行实现，改它修不好任何测试，
# 进 gold_files 只会让 locate_hit 更难达成（与 PytestAdapter 不收 `.pyi` 同理）。
_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs",
                    ".vue", ".svelte")

# 报告里 <testcase name> 用 ` > ` 连接 describe 层级，而 `-t` 匹配的是**空格**
# 连接的那份。这两个**不是**同一个字符串，报告里的名字直接拿去当选择器匹配不到
# 任何用例 —— 已实测：`-t "外层 > 内层 > 普通用例"` 跑了 0 条，
# `-t "外层 内层 普通用例"` 才命中。
_NAME_SEP = " > "

# JS 正则的元字符。`-t` 收的是**正则**不是字面量 —— 已实测：名字里带 `(括号)`
# 时不转义就匹配不到（括号被当成捕获组）。
#
# 不用 Python 的 `re.escape`：它会把空格也转义成 `\ `，而 JS 正则在 `u` 标志下
# 把 `\ ` 当作非法的身份转义。只转 JS 真正认的那些元字符。
_JS_META = re.compile(r"[.*+?^${}()|[\]\\/]")


def _escape(text: str) -> str:
    return _JS_META.sub(lambda m: "\\" + m.group(0), text)


# 找 `package.json` 时跳过的目录。`node_modules` 是硬需求：里面每个包都有自己的
# package.json，而其中不少也依赖 vitest —— 走进去必然认错。
_SKIP_DIRS = frozenset({"node_modules", "dist", "build", "coverage",
                        "target", ".git", "vendor"})


def _has_vitest(pkg: Path) -> bool:
    """这份 package.json 的依赖里**直接**列了 vitest 吗。

    只翻 dependencies / devDependencies，不翻 scripts：`"test": "vitest run"`
    当然也是证据，但反过来 `"test": "jest"` 的工程如果依赖里有 vitest（被别的包
    传递依赖进来），按 scripts 判会漏、按依赖判会误认。依赖里**直接**列了才是
    「这个工程用 vitest」的可靠信号。
    """
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # 读不动 / 不是合法 JSON 就当没有。抛异常的话，异常会从 preflight 或
        # baseline 里钻出来，而那只说明这个 package.json 坏了。
        return False
    if not isinstance(data, dict):
        return False
    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    return "vitest" in deps


def find_pkg_dir(repo: Path) -> str | None:
    """vitest 工程在这个仓库的哪个目录下；没有则 None。`""` 表示仓库根。

    先看根目录，再看**一层**子目录。前后端同仓时前端几乎总是 `web/` /
    `frontend/` / `client/` 这类一级目录，而不写死名字是因为那份名单永远不全，
    漏掉一个的表现是「这个仓库没有前端」—— 与真的没有前端完全无法区分。

    只下一层，不递归：再深就更可能是 monorepo 的某个 package，而那时「哪个才是
    要跑的那套」不是一个探测能回答的问题（`AIFIX_ADAPTERS` 也表达不了），
    猜一个出来只会让另外几个被静默跳过。

    子目录按名字排序后取第一个命中的，让结果**可复现** —— 不定死顺序的话，同一个
    仓库在两台机器上可能选中不同的前端，而两边都不报错。
    """
    if _has_vitest(repo / "package.json"):
        return ""
    for child in sorted(p for p in repo.iterdir() if p.is_dir()):
        if child.name in _SKIP_DIRS or child.name.startswith("."):
            continue
        if _has_vitest(child / "package.json"):
            return child.name
    return None


class VitestAdapter:
    name = "vitest"

    def __init__(self, python: str | None = None,
                 parallel: str | None = None, pkg_dir: str = "",
                 repo: Path | str | None = None) -> None:
        """`python` / `parallel` 都收下、都不用 —— 与 MavenAdapter 同一条理由。

        `adapter_for` 对注册表里每个实现用的是同一行
        `ADAPTERS[name](python=..., parallel=...)`，这里不收的话，任何 JS 工程
        会在**取适配器**时 TypeError，而那发生在 baseline 之前，表现成一次没有
        任何测试输出的崩溃。

        （vitest 本来就默认多线程并行跑，没有对应 `-n` 的东西要传。）

        `pkg_dir`：`package.json` 所在的子目录，`""` 表示仓库根。给了 `repo`
        而没给 `pkg_dir` 时**自己去探**（见 find_pkg_dir）—— 前后端同仓的工程
        前端在 `web/` 下，而 `adapter_for` 没有办法替它猜。

        `repo`：**源仓库**，不是 worktree。两处要用：探 `pkg_dir`，以及
        `prepare()` 从这里把 `node_modules` 借给 worktree（worktree 里没有它，
        它没被 git 跟踪）。收下不用的适配器同样要接这个参数 —— 与 `python` /
        `parallel` 同一条理由，见 `adapter_for`。
        """
        self.repo = Path(repo) if repo is not None else None
        if pkg_dir:
            self.pkg_dir = pkg_dir.strip("/")
        elif self.repo is not None:
            self.pkg_dir = (find_pkg_dir(self.repo) or "").strip("/")
        else:
            self.pkg_dir = ""

    @staticmethod
    def detect(repo: Path) -> bool:
        """根目录**或某个一级子目录**下有依赖 vitest 的 `package.json`。

        判据刻意具体（见 `_has_vitest`）。只看 `package.json` 存在就认领是不行
        的：带一点前端工具链（eslint、prettier、一个构建脚本）的 Python / Java
        工程也有它，而认错的后果不是报错 —— baseline 会去跑一个不存在的 vitest，
        或者跑出 0 个用例，报告写「这个仓库没问题」。

        要看子目录，是因为前后端同仓的工程前端几乎总在 `web/` 这类一级目录下 ——
        只看根目录的话，`AIFIX_ADAPTERS=pytest,vitest` 配了也白配：探测说这里
        没有前端，而那与真的没有前端完全无法区分。
        """
        try:
            return find_pkg_dir(Path(repo)) is not None
        except OSError:
            # 目录读不动（权限、仓库路径根本不存在）就当没有 —— 抛出去的话
            # 异常会从 preflight 里钻出来，而那只说明这个路径有问题。
            return False

    REPORT_NAME = ".aifix-report.xml"
    SCOPED_REPORT_NAME = ".aifix-recheck.xml"

    def _bin(self) -> str:
        """直接调 `node_modules/.bin/vitest`，**不走 `npx`、也不走项目的 test 脚本**。

        不走 npx：包不在时 npx 会尝试联网安装，在沙箱里那是一次几十秒的超时而不是
        一条清楚的错误。

        不走 `npm run test`：那个脚本的内容是项目自己定的。写成 `vitest`（不带
        `run`）就是 **watch 模式** —— 进程永远不退出，整个 run 挂在那里直到墙钟
        闸响。这不是假想：`vitest` 与 `vitest run` 只差一个词，而前者是 vitest
        文档里最常出现的写法。

        相对路径的可执行文件在沙箱里可用 —— 已实测（`LocalSandbox.exec` 以
        `cwd=worktree` 起子进程，路径按子进程的 cwd 解析）。Python 的 subprocess
        文档有一句「不能指定相对 cwd 的程序路径」，那句话说的是 PATH 搜索，
        带斜杠的路径不受它影响。
        """
        base = f"{self.pkg_dir}/" if self.pkg_dir else ""
        return f"{base}node_modules/.bin/vitest"

    def _root(self) -> list[str]:
        return ["--root", self.pkg_dir] if self.pkg_dir else []

    def prepare(self, worktree: Path) -> None:
        """把源仓库的 `node_modules` **借**给 worktree。

        worktree 是从 HEAD 建的，只含被 git 跟踪的文件 —— 而 `node_modules`
        当然没被跟踪。不做这一步，`node_modules/.bin/vitest` 根本不存在，表现是
        「测试进程没能正常跑完」，一句指向目标项目的话。

        **建真目录 + 逐个子项软链，不是软链整个 node_modules。** 这不是洁癖：
        实测（2026-08-04）vitest 跑完会在 `node_modules/.vite` 下写依赖预构建
        缓存。整个目录软链过去的话，那些写**落在源仓库里** —— 而这个项目的地基
        是「agent 的一切改动都发生在 worktree，主工作区绝不被触碰」
        （见 delivery.py 开头那句）。逐个子项软链之后，读走源仓库、写落在
        worktree：已实测 `.vite` 出现在 worktree 侧，源仓库侧没有。

        比 `npm ci` 便宜得多：255 个软链是毫秒级，而 `npm ci` 实测 12 秒
        （还得有缓存或网络）。一次 run 要跑好几遍全量，但 prepare 只做一次。

        **幂等**：目录已经在了就什么都不做。`run_scoped` 与 `run_full_suite`
        各调一次，而一次 run 里它们要跑好几轮。

        源仓库里也没有 `node_modules` 时**什么都不做，不报错**：那时正确的
        动作是让 vitest 自己以「找不到命令」失败 —— 那句报错比我们编一句准确。
        """
        if self.repo is None:
            return
        src = self.repo / self.pkg_dir / "node_modules" if self.pkg_dir \
            else self.repo / "node_modules"
        dst = Path(worktree) / self.pkg_dir / "node_modules" if self.pkg_dir \
            else Path(worktree) / "node_modules"
        if dst.exists() or not src.is_dir():
            return
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for entry in src.iterdir():
                link = dst / entry.name
                if not link.exists():
                    link.symlink_to(entry)
        except OSError:
            # 借不成就算了（权限、文件系统不支持软链）。硬失败在这里没有价值：
            # 下一步 vitest 会以「找不到命令」失败，而那句话更准确。
            return

    def full_test_command(self) -> list[str]:
        return [self._bin(), "run", *self._root(),
                "--reporter=junit", f"--outputFile={self.REPORT_NAME}"]

    def scoped_test_command(self, test_ids: list[str]) -> list[str]:
        """复跑指定用例。**报告里的 id 不能直接当选择器**，要翻译两次。

        1. 文件那一段：`--root` 之下的相对路径，所以要把 `pkg_dir` 前缀去掉。
        2. 用例那一段：`-t` 收的是**正则**，且匹配的是**空格**连接的全名，而报告
           里写的是 ` > ` 连接的。两处都不翻译就一条都匹配不到。

        必须加 `^...$` 锚点。已实测：`-t "外层 前缀"` 同时跑了 `外层 > 前缀` 和
        `外层 > 前缀加长` —— 复跑多跑用例会污染 flaky 确认与红检的判定集合。

        多个用例用正则的选择分支 `^(?:A|B)$` 合成一条 —— `-t` 只能给一次。

        **有文件级 id 时整个不发 `-t`**：那种 id 指的是「这个文件整体没能加载」，
        它名下没有用例名可写进选择分支，而发一个匹配不到它的 `-t` 会让它被跳过，
        于是「文件加载失败」这件事在复跑结果里消失。多跑几条用例是可承受的，把
        一整类失败跑丢不是。
        """
        files: list[str] = []
        names: list[str] = []
        has_file_level = False
        for tid in test_ids:
            path, sep, name = tid.partition("::")
            rel = self._strip_prefix(path)
            if rel not in files:
                files.append(rel)
            if not sep:
                has_file_level = True
            else:
                names.append(f"^{_escape(name.replace(_NAME_SEP, ' '))}$")
        cmd = [self._bin(), "run", *self._root(), *files]
        if names and not has_file_level:
            cmd += ["-t", "|".join(names)]
        return cmd + ["--reporter=junit",
                      f"--outputFile={self.SCOPED_REPORT_NAME}"]

    def _strip_prefix(self, path: str) -> str:
        """仓库相对路径 → `--root` 相对路径。`pkg_dir` 为空时是恒等映射。"""
        if not self.pkg_dir:
            return path
        prefix = self.pkg_dir + "/"
        return path[len(prefix):] if path.startswith(prefix) else path

    def _add_prefix(self, path: str) -> str:
        """`--root` 相对路径 → 仓库相对路径。

        报告里的 `classname` 与栈帧里的路径都相对 `--root`，而这个适配器对外
        （test_id、SourceCandidate.path）一律说**仓库相对**路径 —— 那是核心循环
        与另外两个适配器共同的坐标系，`is_test_path` 和交付记账都按它来。
        已实测：`--root web` 跑出来的 classname 是 `src/a.test.ts`，不是
        `web/src/a.test.ts`。
        """
        return f"{self.pkg_dir}/{path}" if self.pkg_dir else path

    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]:
        """`--outputFile` 是相对 `--root` 解析的，不是相对 cwd —— 已实测：
        从父目录带 `--root web` 跑，报告落在 `web/` 下。"""
        name = self.SCOPED_REPORT_NAME if scoped else self.REPORT_NAME
        path = Path(worktree) / self._add_prefix(name)
        return [path] if path.is_file() else []

    def test_dirs(self) -> list[str]:
        """测试**住在**哪。vitest 的答案是「和源码住在一起」，所以给源码根。

        这个值只用于两件事：写进 reproducer 的提示词（新测试放哪），以及挖任务时
        拆分改动路径。**判断「是不是测试文件」不走它，走 `is_test_path`** ——
        对 vitest 这两个问题的答案不一样，而混用会让守卫拦下整个源码树。
        """
        src = self._add_prefix("src")
        return [src]

    def is_test_path(self, path: str) -> bool:
        """按**后缀**判，不按目录 —— 这正是这个谓词存在的理由。

        `src/components/ChatView.test.tsx` 是测试，`src/components/ChatView.tsx`
        不是，两者同目录。任何基于目录的判据在这里都只能二选一地错。
        """
        return path.endswith(_TEST_SUFFIXES)

    def source_suffixes(self) -> tuple[str, ...]:
        return _SOURCE_SUFFIXES

    def test_selectors(self, test_files: list[str]) -> list[str]:
        """挖任务时把「改动过的测试文件」翻译成 `scoped_test_command` 认得的东西。

        vitest 的文件过滤器就是路径，所以这里只需滤掉非测试文件 —— 测试目录下的
        夹具（快照、fixture JSON）跟着测试一起进 test_files，但把它们放到 vitest
        命令行上会变成「匹配不到任何测试文件」而一个用例都不跑。

        判据用 `is_test_path` 而不是后缀白名单：两处问的是同一个问题，写两份迟早
        漂移成一严一松。
        """
        return [p for p in test_files if self.is_test_path(p)]

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """<testcase> → 仓库相对的 `文件::用例全名`。

        两种形状，都是实测：

        1. **文件级失败** —— `classname == name == "src/broken.test.ts"`。
           整个文件没能加载（import 不到东西、语法错）时 vitest 就发这么一条，
           name 被填成文件路径本身。此时 id 就是文件路径，不能拼 `::`。
        2. **普通用例** —— classname 是文件路径，name 是 ` > ` 连接的 describe
           层级加用例名。

        `file` 一定是 None：vitest 的 <testcase> 不写 file 属性（那是 pytest 的
        xunit1 才有的）。留着这个参数是因为 parse_junit 按位置传三个值。

        id 里保留 ` > ` 而不是就地换成空格：这个字符串会出现在报告、PR 正文和
        issue 回帖里给人看，而 ` > ` 正是 vitest 自己在终端里的写法。翻译成 `-t`
        认的形式是 `scoped_test_command` 的事，不该让给人看的那份跟着变形。
        """
        if not classname:
            return name
        path = self._add_prefix(classname)
        if not name or name == classname:
            return path
        return f"{path}::{name}"

    def is_file_level_id(self, test_id: str) -> bool:
        """文件级 id 就是裸的文件路径，用例 id 一定带 `::`。

        用例名里理论上可以出现 `::`（谁都能这么给测试起名），那样合成出来的 id
        是 `a.test.ts::x::y` —— 仍然带 `::`，仍然判成用例级，正确。
        """
        return "::" not in test_id

    def cases_under(self, file_id: str, test_ids: frozenset[str]) -> set[str]:
        """比的是 `文件::`，不是裸 startswith：`src/ab.test.ts::x` 不属于
        `src/a.test.ts`。"""
        return {i for i in test_ids if i.startswith(file_id + "::")}

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 vitest 的栈里抽出 repo 内的帧，最深的排最前。

        不 reverse：vitest 的栈由深入浅打印（栈顶就是抛出点），本来就是最深的在
        最前 —— 与 Java 一致、与 Python 相反。

        丢掉映射不出真实文件的帧。`SourceCandidate.path` 会原样进模型的提示词，
        一个不存在的路径会让它去读空文件甚至凭空造改动。被丢掉的主要是两类：
        `node_modules` 里的框架帧（vite 的 loadAndTransform 之流，实测出现在
        加载失败的栈里），以及带一长串 `../` 逃出 repo 的路径。
        """
        repo_real = str(Path(repo).resolve())
        out: list[SourceCandidate] = []
        for m in _FRAME.finditer(failure.trace or ""):
            rel = self._resolve(m.group("path"), repo_real)
            if rel is None:
                continue
            out.append(SourceCandidate(
                path=rel, line=int(m.group("line")),
                frame=m.group("fn") or ""))
        return out

    def _resolve(self, raw: str, repo_real: str) -> str | None:
        """帧里的路径 → repo 内的相对路径；不在 repo 内则 None。

        帧里写的是相对 `--root` 的路径（也可能是绝对路径，或带 `../` 指到
        node_modules 里去的），所以先按 `--root` 拼一次再取真实路径。

        存在性检查是收口手段，理由与 PytestAdapter 那边相同：`❯` 开头这个形状
        没有引号做界，被回显的源码里出现类似字样时会被误当成栈帧。多给模型一个
        不存在的候选，比不给候选更糟。
        """
        try:
            p = Path(raw)
            if p.is_absolute():
                real = str(p.resolve())
            else:
                real = str((Path(repo_real) / self._add_prefix(raw)).resolve())
        except OSError:
            return None
        if not real.startswith(repo_real + "/"):
            return None
        if "/node_modules/" in real:
            return None
        if not Path(real).is_file():
            return None
        return PurePosixPath(Path(real).relative_to(repo_real)).as_posix()
