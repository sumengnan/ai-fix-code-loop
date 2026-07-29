from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

from .base import Failure, SourceCandidate

# Python 原生 traceback：  File "/abs/path/calc.py", line 2, in add
# pytest 的 longrepr **不产出**这个形状，但 --tb=native、以及被 pytest 原样
# 转载的嵌套 / 子进程 traceback 会。留着它是为了那些场合。
_NATIVE_FRAME = re.compile(
    r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')

# pytest 自己的帧行，也是 JUnit XML 里**实际**出现的唯一形状。三种收尾：
#   src/mod.py:2: in outer          中间帧，带函数名
#   src/mod.py:5: ZeroDivisionError 最深帧，跟的是异常类型
#   tests/test_mod.py:6:            入口帧，后面什么都没有
# 路径相对 rootdir。行首必须顶格 —— 缩进的行是被回显的源码，里面出现
# `foo.py:3:` 这种字样时不该当成栈帧。尾部一律先收进 rest 再分辨，不在正则
# 里穷举异常名的形状：那种正则每见到一个没想到的收尾就静默少解一帧。
_PYTEST_FRAME = re.compile(
    r"^(?P<path>\S[^:\n]*?):(?P<line>\d+):(?P<rest>[^\n]*)$", re.MULTILINE)

# rest 里带函数名时的形状：` in outer`
_IN_FUNC = re.compile(r"^\s+in\s+(?P<fn>\S+)\s*$")

_MARKERS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py")

# 探测顺序即优先级。`.venv` 在前是因为 uv / poetry / `python -m venv .venv`
# 都用它，`venv` 是更老的写法；两个同时在（换过工具、没删干净）时取新的那个。
_VENV_DIRS = (".venv", "venv")
# Windows 分支**没有在真机上验证过**，仍然写进来：不写等于「Windows 上探测
# 恒空、静默退回 sys.executable」，而写错的代价同样只是探测不到、退回
# sys.executable —— 两边都不比现状差，写上去至少有一半机会是对的。
_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
_VENV_EXE = "python.exe" if os.name == "nt" else "python"


def discover_test_python(repo: Path) -> str | None:
    """在**源仓库**里找它自己的虚拟环境解释器；没有则 None。

    只探源仓库，不探 worktree —— 这不是风格选择：aifix 的 worktree 建在
    `.aifix/runs/<id>/tree`（git worktree），评测建在 `git clone --local` 出来
    的临时目录，两者都**不含** `.venv`（它没被 git 跟踪）。照着 worktree 探，
    探测永远为空，整个功能会静默退化成 sys.executable 而不报任何错。
    解释器路径是绝对的，在别的目录下当 argv[0] 用没有问题。

    要求可执行而不只是「文件存在」：一个不可执行的 `.venv/bin/python`
    （权限被改过、venv 拷贝坏了）拿去当命令会在 exec 时 PermissionError，
    而那发生在 baseline 里 —— 用户看到的是「测试没跑成」，一句指向错误方向
    的话。退回 sys.executable 不比现状差，所以这里宁可当作没找到。
    """
    for d in _VENV_DIRS:
        p = Path(repo) / d / _VENV_BIN / _VENV_EXE
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def resolve_test_python(repo: Path, configured: str | None = None) -> str | None:
    """跑目标项目测试该用哪个解释器：显式配置 > 源仓库的 venv > None。

    返回 None 表示两条来源都空，由适配器退回 `sys.executable`（改造之前的
    唯一行为）——这条回退不能去掉：目标项目没有 venv、或者依赖本来就装在
    当前解释器里（uv tool 的 overlay、CI 的单一环境）时，它是对的。

    显式配置不做存在性检查，只做 `~` 展开：拒绝的动作放在 preflight，那里
    才有「中止整个 run 并告诉用户为什么」的位置；在这里抛的话，异常会从
    detect / fix / verify 三个节点的任意一个里钻出来。
    """
    if configured:
        return os.path.expanduser(configured)
    return discover_test_python(repo)


# 探测目标包时跳过的目录名。`tests` 是测试自己的包，不是被验证的产品代码；
# 其余几个是常见的非产品目录，它们即使解析到 worktree 之外也说明不了什么。
_NOT_PRODUCT = {"tests", "test", "docs", "doc", "examples", "example",
                "scripts", "benchmarks", "build", "dist"}

# 在目标解释器里问一句「这些顶层包会从哪个文件导入」。用 find_spec 而不是
# import：不执行包体，快，且一个包的 import 副作用不会影响下一个的答案。
_PROBE_SRC = """
import importlib.util, json, sys
out = {}
for name in sys.argv[1:]:
    try:
        spec = importlib.util.find_spec(name)
    except BaseException:
        spec = None
    origin = None
    if spec is not None:
        origin = spec.origin
        if origin in (None, "namespace"):
            locs = list(spec.submodule_search_locations or ())
            origin = locs[0] if locs else None
    out[name] = origin
print(json.dumps(out))
"""


def _candidate_packages(worktree: Path) -> list[str]:
    """worktree 里能当锚点的顶层包名：根目录与 `src/` 下带 `__init__.py` 的。"""
    names: list[str] = []
    for parent in (worktree, worktree / "src"):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            n = child.name
            if (n in _NOT_PRODUCT or n.startswith((".", "_"))
                    or not n.isidentifier() or n in names):
                continue
            if (child / "__init__.py").is_file():
                names.append(n)
    return names[:20]


def _ini_pythonpath(worktree: Path) -> list[str]:
    """读 pytest 配置里的 `pythonpath`，还原成 worktree 下的绝对路径。

    这一项是**决定性**的：它让 pytest 把 worktree 的源码目录插到 sys.path
    最前，从而盖过 site-packages 里那条指向源仓库的可编辑安装记录。不读它
    的话，凡是 src 布局 + 配了 pythonpath 的项目（正常且安全的配置）都会被
    下面的探测误报一次 —— 一条在最常见的健康配置上就会响的警告，等于没有。
    """
    out: list[str] = []
    raw: object = None
    pyproject = worktree / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            raw = data.get("tool", {}).get("pytest", {}).get(
                "ini_options", {}).get("pythonpath")
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            raw = None
    if raw is None:
        # ini 家族里 pytest 各用一个 section 名：pytest.ini / tox.ini 是
        # [pytest]，setup.cfg 是 [tool:pytest]（[pytest] 在 setup.cfg 里被
        # pytest 明确拒绝）。值是空白分隔的多行字符串。
        for fname, section in (("pytest.ini", "pytest"), ("tox.ini", "pytest"),
                               ("setup.cfg", "tool:pytest")):
            f = worktree / fname
            if not f.is_file():
                continue
            # interpolation=None：setup.cfg / tox.ini 里出现 `%` 是常事
            # （`%(name)s` 之外的裸 `%` 会让默认的 BasicInterpolation 抛），
            # 而这里只是取一个值，不需要插值。
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read(f, encoding="utf-8")
                raw = cp.get(section, "pythonpath")
            except (OSError, configparser.Error, UnicodeDecodeError):
                raw = None
            if raw is not None:
                break
    if isinstance(raw, str):
        raw = raw.split()
    for item in raw or ():
        if isinstance(item, str):
            out.append(str((worktree / item).resolve()))
    return out


def imports_outside_worktree(python: str,
                             worktree: Path) -> list[tuple[str, str]]:
    """目标项目的顶层包在这个解释器里解析到了 worktree 之外吗。

    存在的理由（这是换解释器换来的**真实**风险）：目标项目如果把自己可编辑
    安装（`pip install -e .`）进了那个 venv，site-packages 里会留一条指向
    **源仓库**的路径记录。于是 `import <目标包>` 可能解析到源仓库那份
    **没打补丁**的代码，而不是 worktree 里打了补丁的那份 —— 测试照跑、照绿，
    验证却完全失去意义。这是这个项目最怕的那种失效：不崩溃、不报错、
    只有结论是假的。

    这里**不解决**它（那要么接管目标项目的安装方式，要么改写它的 sys.path，
    两件都超出适配器的职权），只负责在它发生时出声。

    **这是一个近似，不是保证**：它按 `python -c` 复现 pytest 的 sys.path
    前几项（cwd + ini 里的 pythonpath + 环境里的 PYTHONPATH），但复现不了
    conftest.py 里手写的 sys.path 改动、`--import-mode=importlib` 的细节、
    以及 rootdir 之外的插件。所以它只报警、不拦截，且返回空**不等于**安全。
    任何异常都吞掉返回空：这道探测的价值是提醒，代价不能是多一条崩溃路径。
    """
    wt = Path(worktree).resolve()
    names = _candidate_packages(wt)
    if not names:
        return []
    env = dict(os.environ)
    extra = _ini_pythonpath(wt)
    if extra:
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [*extra, *([old] if old else [])])
    try:
        res = subprocess.run([python, "-c", _PROBE_SRC, *names],
                             cwd=str(wt), capture_output=True, text=True,
                             timeout=60, env=env)
        found = json.loads(res.stdout or "{}")
    except Exception:
        return []
    hits: list[tuple[str, str]] = []
    for name in names:
        origin = found.get(name)
        if not isinstance(origin, str) or not origin:
            # 解析不到（缺依赖 / 命名空间包没有实体路径）不是这道探测要说的
            # 事：那会以收集错误的形式响亮地出现在测试结果里。
            continue
        try:
            real = Path(origin).resolve()
        except OSError:
            continue
        if not real.is_relative_to(wt):
            hits.append((name, str(real)))
    return hits


class PytestAdapter:
    name = "pytest"

    def __init__(self, python: str | None = None) -> None:
        """python：跑测试用的解释器；None 表示退回 `sys.executable`。

        做成构造参数而不是 `full_test_command(python=...)` 那样的方法参数，
        是为了不动 `ProjectAdapter` 协议：核心循环有四个节点各自取一次适配器，
        改协议要连 MavenAdapter 和每一个调用点一起改，而 Maven 压根不需要它
        （`mvn` 是外部命令，不走 Python 解释器）。注入点因此收在
        `nodes.baseline.adapter_from_state` 一处。
        """
        self.python = python or sys.executable

    @staticmethod
    def detect(repo: Path) -> bool:
        if any((repo / m).is_file() for m in _MARKERS):
            return True
        return (repo / "tests").is_dir()

    # -B 不写 __pycache__，-p no:cacheprovider 不写 .pytest_cache：两者都是
    # 为了跑完测试之后，worktree 里除了被跟踪文件的改动之外什么都不多出来。
    #
    # 理由**不是**「会被扫进交付分支」——Worktree.commit 只
    # `git add -- <ApplyPatchTool 记账过的路径>`，这个仓库里没有 git add -A
    # 这条交付路径（delivery.Worktree.commit 的 docstring 写着绝不用；
    # tests/test_maven_e2e.py 有一条真跑 mvn 的验收：整个 target/ 都没进树）。
    # 真实理由是未跟踪产物**跨状态存活**：同一个 worktree 会被
    # `git checkout --force` 在 C^ 和 C 之间来回切（eval/mine.verify_commit），
    # 而 checkout 不碰未跟踪文件，上一跑留下的东西原样活到下一跑。陈旧报告被
    # 下一跑当成自己的结果就是这个机制（见 nodes/baseline._rm_reports 与
    # MavenAdapter 命令里的 clean）；压根不写出来的产物，不需要任何人记得去
    # 清，也就不存在清漏。顺带，`git status` 不被这些目录淹掉，交付前想看一眼
    # 工作区到底动了什么才看得清。
    #
    # -o junit_family=xunit1：xunit2（pytest 的默认）**不写** <testcase file=...>，
    # 而 file 是把 junit 报告里的用例还原成可重跑 node id 的唯一可靠依据。
    # 已实测（pytest 9.1.1）：xunit1 多出 file/line 两个属性，其余结构
    # （skipped / failure / error / message）与 xunit2 完全一致，且无
    # deprecation 警告。`-o` 覆盖目标项目 ini 里的设置。
    _BASE = ["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "junit_family=xunit1"]

    # 复跑单独一份报告：flaky 确认发生在全量结果还要继续用的时候，同名会把
    # baseline 那份覆盖成只含两三个用例的报告，随后又被清理删掉。
    REPORT_NAME = ".aifix-report.xml"
    SCOPED_REPORT_NAME = ".aifix-recheck.xml"

    def full_test_command(self) -> list[str]:
        return [self.python, *self._BASE, f"--junitxml={self.REPORT_NAME}"]

    def scoped_test_command(self, test_ids: list[str]) -> list[str]:
        return [self.python, *self._BASE,
                f"--junitxml={self.SCOPED_REPORT_NAME}", *test_ids]

    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]:
        name = self.SCOPED_REPORT_NAME if scoped else self.REPORT_NAME
        path = Path(worktree) / name
        return [path] if path.is_file() else []

    def test_dirs(self) -> list[str]:
        return ["tests", "test"]

    def source_suffixes(self) -> tuple[str, ...]:
        # 不含 `.pyi`：存根文件里没有可执行的实现，改它修不好任何测试，
        # 进 gold_files 只会让 locate_hit 变得更难达成。
        return (".py",)

    def test_selectors(self, test_files: list[str]) -> list[str]:
        """pytest 的选择器就是路径本身，只需把非 `.py` 滤掉。

        测试目录下的夹具（数据文件、快照、配置片段）会跟着测试一起进
        test_files（见 eval/mine.split_paths），它们必须被 materialize
        嫁接，但出现在 pytest 命令行上会让收集整轮中止（exit 4）——
        一个用例都不跑，写出的是一份 tests="0" 的空报告。
        """
        return [p for p in test_files if PurePosixPath(p).suffix == ".py"]

    def make_test_id(self, classname: str, name: str, file: str | None) -> str:
        """把 junit 报告里的一条 <testcase> 还原成 pytest 认得的 node id。

        三种形状，来源都是实测：

        1. 收集错误 —— classname="" / name="tests.test_x" / file="tests/test_x.py"。
           整个文件没能导入，pytest 发的是一条文件级 <error>。此时可重跑的
           node id 就是文件路径本身，不能拼 `::`。
        2. 类内测试 —— classname="tests.test_foo.TestBar"。pytest 要的是
           `tests/test_foo.py::TestBar::test_baz`，classname 尾部超出模块
           路径的那些段就是类名链（支持嵌套类）。
        3. 模块级测试 —— classname="tests.test_foo"，直接 `文件::用例`。

        无效 id 的代价不是报错而是静默：它进了 pytest 命令行，pytest 在收集
        阶段整轮中止（exit 4），一个用例都不跑，写出一份 tests="0" 的空报告 ——
        报告存在，require_report 查不出异常，看起来像「全部复跑通过」。
        """
        if not classname:
            return file or name
        if file:
            stem = PurePosixPath(file).with_suffix("").parts
            cls = classname.split(".")
            # classname 前缀与文件路径对不上时（rootdir 不同等），退回不带
            # 类名的形式 —— 与本函数改造前的行为一致，不会更糟
            classes = list(cls[len(stem):]) if list(cls[:len(stem)]) == list(stem) else []
            return "::".join([file, *classes, name])
        # file 缺失（别的适配器 / xunit1 哪天消失）：从尾部剥掉首字母大写的段。
        # pytest 默认 python_classes = Test*，类名必然大写开头；模块名按
        # PEP 8 小写。第一段永不剥 —— 全大写的退化输入不能把路径剥空。
        parts = classname.split(".")
        i = len(parts)
        while i > 1 and parts[i - 1][:1].isupper():
            i -= 1
        path = "/".join(parts[:i]) + ".py"
        return "::".join([path, *parts[i:], name])

    def is_file_level_id(self, test_id: str) -> bool:
        """收集错误发出的 id 就是文件路径本身，用例 id 一定带 `::`。

        见 make_test_id 的第 1 种形状：整个文件没能导入时 pytest 发一条
        文件级 <error>，可重跑的 node id 是文件路径，不能拼 `::`。
        """
        return "::" not in test_id

    def cases_under(self, file_id: str, test_ids: frozenset[str]) -> set[str]:
        """比的是 `文件::`，不是裸 startswith。

        裸前缀会把 `tests/test_xyz.py::t` 算进 `tests/test_x.py` 名下 ——
        那个文件红着，这个文件就永远判不出「整体变绿」。
        """
        return {i for i in test_ids if i.startswith(file_id + "::")}

    def _resolve(self, raw: str, repo_real: str) -> str | None:
        """把帧里的路径还原成 repo 内的相对路径；不在 repo 内则 None。

        pytest 写的是**相对 rootdir** 的路径，原生 traceback 写的是绝对路径，
        两种都要认，所以相对的先按 repo 拼一次。

        存在性检查是 pytest 那个形状的收口手段：它没有引号做界，`a.py:3:`
        这种字样可能出现在被回显的源码或断言文本里。多给 Detector 一个不存在
        的候选，比不给候选更糟 —— 它会照着那个路径去编造根因。
        """
        try:
            p = Path(raw)
            real = str((p if p.is_absolute() else Path(repo_real) / p).resolve())
        except OSError:
            return None
        if not real.startswith(repo_real + "/"):
            return None
        if "site-packages" in real or "/dist-packages/" in real:
            return None
        if not Path(real).is_file():
            return None
        return str(Path(real).relative_to(repo_real))

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 traceback 抽出 repo 内部帧，最深的排最前。

        两种帧形状都扫，按它们在文本里出现的先后合并 —— pytest 的
        `src/mod.py:2: in outer` 是 JUnit 报告里实际出现的那种，原生的
        `File "...", line N, in fn` 只在 --tb=native 与转载的 traceback 里出现。

        只认前者会漏掉后者，只认后者……就是这个函数改造前的样子：真实数据上
        一帧都匹配不到，Detector 每次都拿到「未能从栈帧定位到 repo 内的源码」
        然后盲猜路径。当时的单元测试喂的是手写的原生 traceback，所以全绿。

        纯断言失败拿不到源码帧，这不是解析能补的：被调函数正常返回了，栈上
        没有它。那种情况下返回的只有测试文件那一帧，调用方要据此知道
        Detector 是在无锚点地猜（见 nodes/detect.py 的 suspect_anchored）。
        """
        repo_real = str(Path(repo).resolve())
        hits: list[tuple[int, SourceCandidate]] = []
        for pattern in (_PYTEST_FRAME, _NATIVE_FRAME):
            for m in pattern.finditer(failure.trace):
                path = self._resolve(m.group("path"), repo_real)
                if path is None:
                    continue
                groups = m.groupdict()
                if "fn" in groups:                      # 原生 traceback
                    fn = groups["fn"]
                else:                                   # pytest 的帧行
                    tail = _IN_FUNC.match(groups["rest"] or "")
                    # 最深帧的尾部是异常类型、入口帧的尾部是空 —— 都没有函数名
                    fn = tail.group("fn") if tail else ""
                hits.append((m.start(), SourceCandidate(
                    path=path, line=int(m.group("line")), frame=fn)))
        # 先按出现顺序（由浅入深）排稳，再整体反转 —— 最深的最可疑
        hits.sort(key=lambda h: h[0])
        return [c for _, c in reversed(hits)]
