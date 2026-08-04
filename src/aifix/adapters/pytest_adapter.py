from __future__ import annotations

import ast
import configparser
import functools
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

from ..signals import under_dirs
from .base import Failure, SourceCandidate

# 走不进去的目录。`.venv` 是硬需求而不是优化：仓库里的虚拟环境有上万个 .py，
# 且它们一个都不是产品代码 —— 漏掉它，`from calc import add` 会匹配到
# site-packages 里某个同名模块，Detector 拿到的候选是别人的库。
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", ".tox", ".nox", "__pycache__", "node_modules",
    "site-packages", "dist-packages", ".mypy_cache", ".pytest_cache", "build",
})
# 索引上限。走目录树是每个失败一次的开销，正常工程远够用；巨型 monorepo 上
# 与其扫穿，不如退回「没有 import 锚点」—— 那是这条退路引入前的行为，
# 不会更糟。
_MAX_INDEXED = 20_000
# `pkg/__init__.py` → `pkg/api.py` → `pkg/_impl/core.py` 这种两跳转发是常见
# 的；再深就更可能是解析绕进了环，而不是真的还有一层门面。
_MAX_REEXPORT_HOPS = 3

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


# 「不并行」的几种写法。`"0"` 与 `"off"` 是人会写的，空串是 Actions 给的
# （`env: X: ${{ vars.Y }}` 在 Y 未设置时给空串而不是不设）。
_SERIAL = frozenset({"", "off", "no", "false", "0", "none", "null", "1"})


def _clean_parallel(value: str | None) -> str | None:
    """把 `parallel` 洗成能直接跟在 `-n` 后面的值；不并行时返回 None。

    `"1"` 也归进串行：`-n 1` 会起一个 worker 进程，比不起还慢（多一层 fork
    与结果汇总），而写这个值的人想表达的就是「别并行」。
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    return None if v in _SERIAL else v


# 探测目标解释器里有没有 pytest-xdist。用 find_spec 而不是 import：不执行包体，
# 快，而且一个包的 import 副作用不会影响后面。
_XDIST_PROBE = (
    "import importlib.util as u, sys; "
    "sys.exit(0 if u.find_spec('xdist') else 1)")


@functools.lru_cache(maxsize=8)
def has_xdist(python: str, timeout: float = 20.0) -> bool:
    """目标解释器里能不能用 pytest-xdist。探不动一律当没有。

    带缓存：`adapter_from_state` 被四个节点各调一次、每轮 attempt 各一遍，
    而这个答案在一次 run 内不会变。缓存键是解释器路径，所以换解释器仍会重探。

    **在目标解释器里探，不在 aifix 自己的解释器里探** —— 跑测试的是前者，
    而这个项目已经为「拿 aifix 的解释器去代表目标项目」栽过一次（那次是
    11 个 collection error）。

    任何异常都当成没有：探测失败时退回串行，那是并行化之前的行为，不会更糟。
    """
    try:
        return subprocess.run(
            [python, "-c", _XDIST_PROBE],
            capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_test_parallel(python: str | None,
                          configured: str | None = None) -> str | None:
    """全量测试用几个 worker：显式配置 > 探测 > None（串行）。

    `configured` 为 `"auto"` 时才去探；给了具体数字就直接用（那是用户明确的
    要求，不该被一次探测推翻）。探不到 xdist 就返回 None —— 静默退回串行，
    而不是发一条 `-n auto` 让 pytest 以「unrecognized arguments」当场退出。
    """
    cleaned = _clean_parallel(configured)
    if cleaned is None:
        return None
    if cleaned != "auto":
        return cleaned
    return "auto" if python and has_xdist(python) else None


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


def _read_text(path: Path) -> str | None:
    """读不出来就当没有 —— 编码坏掉的文件不该炸掉整次定位。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _repo_modules(repo_real: str) -> dict[str, str]:
    """{可导入的模块名: 仓库内相对路径}。

    一个文件贡献它路径的**每一个后缀**：`src/shopcart/cart.py` 同时登记成
    `shopcart.cart` 和 `cart`（以及 `src.shopcart.cart`）。因为「测试写的
    模块名对应哪一段路径」取决于 pythonpath / src 布局 / 有没有装成包，
    在这里判定不了；登记全部后缀让匹配自己去选，比赌某一种布局稳。

    冲突时**短路径优先**：同名模块存在于多处时，靠近仓库根的那个更可能是
    被测的产品代码。这是启发式，不是保证。
    """
    index: dict[str, str] = {}
    count = 0
    for root, dirs, files in os.walk(repo_real):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            count += 1
            if count > _MAX_INDEXED:
                return index
            rel = str(Path(root, fn).relative_to(repo_real))
            parts = PurePosixPath(rel).parts
            # `pkg/__init__.py` 导入时写的是 `pkg`，不含末段
            stem = list(parts[:-1]) if parts[-1] == "__init__.py" \
                else [*parts[:-1], parts[-1][:-3]]
            for i in range(len(stem)):
                mod = ".".join(stem[i:])
                old = index.get(mod)
                if old is None or len(rel) < len(old):
                    index[mod] = rel
    return index


def _imports_of(source: str) -> list[tuple[str, list[str]]]:
    """[(模块名, 从它导入的符号名)]，按出现顺序。

    相对 import（level > 0）跳过：解析它要知道文件自己的包名，而那取决于
    rootdir / pythonpath / 有没有 __init__.py，猜错就是给 Detector 一个不
    存在的候选 —— 比不给候选更糟。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            out.append((node.module, [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            # `import a.b as c` —— 模块是 a.b，没有「从中导入的符号」
            out.extend((a.name, []) for a in node.names)
    # stdlib 的模块名不可能是本仓库的产品代码。不靠「匹配不到就自然掉队」
    # 兜底：仓库里真有个 `json.py` 时，`import json` 会命中它 —— 那多半
    # 是巧合而不是证据。
    return [(m, n) for m, n in out
            if m.split(".")[0] not in sys.stdlib_module_names]


def _names_in(name: str, haystack: str) -> bool:
    """符号名在失败文本里被点过名。按词边界比，`add` 不该被 `address` 命中。"""
    return re.search(rf"\b{re.escape(name)}\b", haystack) is not None


def _defined_in(source: str, wanted: set[str]) -> tuple[int, str]:
    """符号在这份源码里的定义行。没定义返回 (1, "")。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1, ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name in wanted:
            return node.lineno, node.name
    return 1, ""


def _forwarded_to(source: str, rel: str, wanted: set[str],
                  index: dict[str, str], repo_real: str) -> str | None:
    """这份源码把 wanted 里的符号转发给了哪个模块（仓库内相对路径）。

    包内相对 import 在这里**是可解的**，与测试文件里的相对 import 不同：
    参照系是这个文件自己的目录，`.cart` 就是同级的 cart.py，纯路径运算，
    不需要猜 rootdir / pythonpath。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    base = PurePosixPath(rel).parent
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not any(a.name in wanted for a in node.names):
            continue
        if node.level:
            # level=1 是本包，每多一级往上一层。`__init__.py` 的包目录就是
            # 它自己所在的目录，普通模块同理，所以两者都从 parent 起算。
            here = base
            for _ in range(node.level - 1):
                here = here.parent
            stem = here / (node.module or "").replace(".", "/")
            for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                if (Path(repo_real) / cand).is_file():
                    return cand
        elif node.module:
            hit = index.get(node.module)
            if hit is not None:
                return hit
    return None


def _anchor_for(repo_real: str, rel: str, names: list[str],
                index: dict[str, str]) -> tuple[str, int, str]:
    """顺着 re-export 找到符号**真正定义**的那个文件。

    动机是实测：`from shopcart import most_expensive` 解析到
    `src/shopcart/__init__.py`，而那个文件只有一行 `from .cart import ...`
    转发，逻辑全在 `src/shopcart/cart.py`。停在 `__init__.py` 给出的是一个
    不含任何逻辑的文件 —— 比模型自己猜好不了多少，gold_files 也对不上。

    返回 (相对路径, 行号, 符号名)。追不到定义时返回**追到的最后一处**：
    多跳一步至少换来一个更接近实现的文件。跳数封顶且带 seen 集，互相转发
    的两个模块不会把定位转成死循环。
    """
    if not names:
        return rel, 1, ""
    wanted = set(names)
    cur, seen = rel, {rel}
    for _ in range(_MAX_REEXPORT_HOPS):
        src = _read_text(Path(repo_real) / cur)
        if src is None:
            break
        line, fn = _defined_in(src, wanted)
        if fn:
            return cur, line, fn
        nxt = _forwarded_to(src, cur, wanted, index, repo_real)
        if nxt is None or nxt in seen or not (Path(repo_real) / nxt).is_file():
            break
        seen.add(nxt)
        cur = nxt
    return cur, 1, ""


class PytestAdapter:
    name = "pytest"

    def __init__(self, python: str | None = None,
                 parallel: str | None = None,
                 repo: Path | str | None = None) -> None:
        """python：跑测试用的解释器；None 表示退回 `sys.executable`。

        repo 收下不用：pytest 侧的依赖来自那个**worktree 之外**的解释器，
        worktree 里不需要补任何东西（见 prepare）。接它是接口对齐的代价 ——
        `adapter_for` 对注册表里每个实现用的是同一行调用。

        做成构造参数而不是 `full_test_command(python=...)` 那样的方法参数，
        是为了不动 `ProjectAdapter` 协议：核心循环有四个节点各自取一次适配器，
        改协议要连 MavenAdapter 和每一个调用点一起改，而 Maven 压根不需要它
        （`mvn` 是外部命令，不走 Python 解释器）。注入点因此收在
        `nodes.baseline.adapter_from_state` 一处。

        parallel：全量测试的 pytest-xdist worker 数（`"auto"` 或一个数字）。
        None / 空串 / `"off"` / `"0"` 一律串行，**只作用于全量**（见
        `full_test_command`）。

        空串必须当没设：GitHub Actions 里 `env: X: ${{ vars.Y }}` 在 Y 未设置
        时给的是**空串**而不是不设 —— 不接这一手，`-n ''` 会被发给 pytest，而
        它以「argument -n: invalid int value」当场退出，表现成整个 baseline
        跑不起来（与 `reproducer_thinking` 那处是同一个坑）。
        """
        self.python = python or sys.executable
        self.parallel = _clean_parallel(parallel)

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
        """全量。配了 `parallel` 就交给 pytest-xdist 并行跑。

        **一次 run 要跑好几遍全量**（1 次 baseline + 每轮 verify 各 1 次），
        所以这一条是整个系统里最值钱的一处提速。实测（2026-08-03，issue #9 的
        真跑）：串行跑本仓库的 944 个用例，一次 run 花了 28 分半，而其中绝大
        部分是那三遍全量。

        **只在这里并行，scoped 不并行**：复跑一次就一两个用例，起 N 个 worker
        是纯开销 —— xdist 要 fork 进程、收集、分发、汇总，而被分发的只有一个
        用例。flaky 确认那一跑尤其怕这个，它本来只花几秒。
        """
        par = ["-n", self.parallel] if self.parallel else []
        return [self.python, *self._BASE, *par,
                f"--junitxml={self.REPORT_NAME}"]

    def scoped_test_command(self, test_ids: list[str]) -> list[str]:
        return [self.python, *self._BASE,
                f"--junitxml={self.SCOPED_REPORT_NAME}", *test_ids]

    def report_paths(self, worktree: Path, scoped: bool = False) -> list[Path]:
        name = self.SCOPED_REPORT_NAME if scoped else self.REPORT_NAME
        path = Path(worktree) / name
        return [path] if path.is_file() else []

    def test_dirs(self) -> list[str]:
        return ["tests", "test"]

    def prepare(self, worktree: Path) -> None:
        """什么都不用做。

        依赖来自**worktree 之外**的那个解释器（绝对路径，见
        `resolve_test_python`），worktree 里本来就不需要有 `.venv`。
        """

    def is_test_path(self, path: str) -> bool:
        """pytest 的测试住在目录里，判据就是「在不在测试目录之下」。

        与改造前各调用点写的 `under_dirs(path, adapter.test_dirs())` 逐字节
        相同 —— 这个方法存在的理由是 vitest（测试与源码同目录、靠后缀区分），
        不是这里。
        """
        return under_dirs(path, self.test_dirs())

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

    def example_test_id(self) -> str:
        """两种形状都给：模块级函数与类内方法。

        只给一种的话，模型写类内测试时会照着模块级那条拼，得到
        `tests/test_x.py::test_y` 而实际用例在类里 —— 那个 id 跑不出结果。
        """
        return ("tests/test_calc.py::test_add"
                "（类内的是 tests/test_calc.py::TestCalc::test_add）")

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
        没有它。那种情况下退到测试文件的 import（见 `_import_candidates`）——
        仍然是确定性证据，只是弱一档，用 origin 标出来。
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
        frames = [c for _, c in reversed(hits)]

        # 栈帧里已经有源码文件时**不掺 import**：那是更弱的证据，混进来只会
        # 稀释真锚点，把「失败穿过这里」和「测试用到了这个模块」摆成同一档。
        if any(not self.is_test_path(c.path) for c in frames):
            return frames
        imported = self._import_candidates(failure, frames, repo_real)
        # 源码候选排在测试文件那一帧前面 —— 「按可疑度排序」，而断言所在的
        # 那一行恰恰是最不可能藏着缺陷的地方（它只是发现问题的地方）。
        return imported + frames

    def _import_candidates(self, failure: Failure,
                           frames: list[SourceCandidate],
                           repo_real: str) -> list[SourceCandidate]:
        """从测试文件 import 了什么，反推嫌疑源码文件。零 LLM，确定性。

        动机是实测：纯断言失败下 Detector 的输入里**一个产品代码文件都没有**
        （它无工具、单步，连仓库有哪些目录都看不到），只能按包名猜路径。同一
        个失败连跑三次给出 `cart.py` / `cart.py` / `src/cart.py`，真实路径是
        `src/shopcart/cart.py` —— 三次都没对，而按分段后缀判定前两个算命中、
        第三个算未命中。那一列量到的是运气。

        而证据一直躺在测试文件顶上：`from shopcart.cart import most_expensive`
        既给出模块，也给出被测符号。

        三步：
        1. ast 解析测试文件的 import，取模块名与导入的符号名；
        2. 模块名按**分段后缀**匹配仓库里的 .py（`shopcart.cart` →
           `src/shopcart/cart.py`），src 布局下裸拼 `repo / 模块路径` 找不到，
           那正是模型猜不中的那段前缀；
        3. 断言文本里点过名的符号，它所在的模块排前面，并把行号定到那个符号
           的 def 行 —— 只给文件不给符号的话，一个 import 五个模块的测试
           产出的是一份没有次序的清单，跟没有锚点差不了多少。

        故意不处理相对 import（`from .cart import x`）：解析它要知道测试文件
        自己的包名，而 rootdir / pythonpath / 有没有 __init__.py 都会改变答案，
        猜错就是给 Detector 一个不存在的候选 —— 那比不给候选更糟（同
        `_resolve` 的存在性检查）。测试文件用相对 import 本来也罕见。
        """
        out: list[tuple[int, int, SourceCandidate]] = []
        haystack = f"{failure.message}\n{failure.trace}"
        index = _repo_modules(repo_real)
        seen: set[str] = set()
        for order, frame in enumerate(frames):
            src = _read_text(Path(repo_real) / frame.path)
            if src is None:
                continue
            for module, names in _imports_of(src):
                entry = index.get(module)
                if entry is None:
                    continue
                # 被失败文本点过名的符号 —— 用它同时定名次、追转发、定行号
                named = [n for n in names if _names_in(n, haystack)]
                path, line, fn = _anchor_for(repo_real, entry, named, index)
                # 去重按**追完之后**的路径：同一个包的两条 import 会汇到同一
                # 个实现文件，按入口去重的话它会重复出现在候选列表里
                if path in seen or self.is_test_path(path):
                    continue
                seen.add(path)
                # 排序键：点名数降序（负号），其次保持 import 出现顺序，
                # 让「没被点名」的候选仍然进列表、只是靠后
                out.append((-len(named), order * 1000 + len(out),
                            SourceCandidate(path=path, line=line, frame=fn,
                                            origin="import")))
        out.sort(key=lambda t: (t[0], t[1]))
        return [c for _, _, c in out]
