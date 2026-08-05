"""补丁合理性的静态信号。零模型调用，纯 AST。

不改判定 —— 三态判定仍然只看测试结果。这里做的是给**人**一个信号：
规格 §1 定的是「审查者是人」，但 M3 真实验收里报告没有给人任何值得多看
一眼的东西。模型把 add 改成有状态函数去满足一个自相矛盾的断言、顺手删掉
无测试覆盖的 mul，每一道守卫都正常工作了：它们检查的都是 agent 的**行为**
（改没改测试、diff 大不大、越没越界），没有一道检查补丁的**合理性**。

明确的局限：静态信号挡不住「在测试覆盖范围内把实现改成特例硬编码」。那
需要覆盖率差分甚至语义分析。这不是一个能靠加信号彻底解决的问题 —— 它是
测试覆盖率作为天花板的直接后果。
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# 只对 Python 做 AST 分析；其它后缀照样进 files_outside_suspect。
_PY = ".py"

# 「构造一个空的可变容器」的三个内置名。右值是它们的调用时，与写字面量
# 等价 —— 只认字面量会漏掉 `CACHE = dict` 这种同义写法。
_MUTABLE_BUILTINS = frozenset({"list", "dict", "set"})


def _path_parts(path: str) -> tuple[str, ...]:
    """把路径规整成 POSIX 分段序列，供后缀匹配用。

    仓库里的路径都是 git 产出的 POSIX 形式，但模型给出的 suspect_file 可能带
    `./` 前缀或 `\\` 分隔符（尤其是习惯 Windows 风格的模型）。先统一分隔符、
    去掉前导 `./`，再切分，两侧才能在同一套坐标系里比较。
    """
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).parts


def same_file(a: str | None, b: str | None) -> bool:
    """两条路径是否指同一个文件：路径分段后缀匹配。

    这一份判定同时服务两处 —— `eval/runner.locate_hit`（suspect 有没有命中
    gold_files）与本模块的 `files_outside_suspect`（改动有没有落在 suspect
    之外）。两处问的是同一个问题「模型说的那个文件和我手上这个是不是同一
    个」，实现必须只有一份：复制出来的两份会各自漂移，届时同一对路径在定
    位准确率里算命中、在越界信号里算越界，报告自相矛盾。

    规则细节与它的来历见 `eval/runner.locate_hit` 的 docstring：为什么按分
    段比而不是裸 `endswith`（`xmine.py` 的假阳性），以及为什么不放宽到只比
    文件名（`other/mine.py` 会蒙对）。
    """
    if not a or not b:
        return False
    a_parts, b_parts = _path_parts(a), _path_parts(b)
    if not a_parts or not b_parts:
        return False
    shorter, longer = ((a_parts, b_parts) if len(a_parts) <= len(b_parts)
                       else (b_parts, a_parts))
    return longer[len(longer) - len(shorter):] == shorter


def under_dirs(path: str, dirs: list[str]) -> bool:
    """path 是否落在 dirs 里某个目录之下：按路径**分段**比前缀。

    这一份判定同时服务 `tools/patch.py` 的「不许改测试文件」守卫与
    `eval/mine.split_paths` 的「测试侧 / 源文件」拆分。两处问的是同一个问题
    「这个文件在不在测试目录里」，实现必须只有一份 —— 复制出来的两份会各自
    漂移：本分支之前 mine 已经升级成分段前缀，patch.py 还停在 `parts[0] in
    test_dirs`，而 M5 的 MavenAdapter 用的是标准布局 `src/test/java/...`，
    test_dirs 会是 `["src/test"]`。首段是 `src`，守卫直接放行，这个项目最核
    心的一道守卫静默失效。

    为什么必须按分段比、不能用裸 `startswith`：`"testdata/x.py".startswith(
    "test")` 是 True，但 `testdata` 不是 `test` 目录。

    空字符串一律跳过：它的分段序列是 ，是任何路径的前缀，会让守卫拦下
    一切改动 —— 配置里多一个空项就把整个系统变成只读的。

    **大小写不敏感**，这是个取舍。macOS 与 Windows 的文件系统默认不区分大小
    写：`a/TESTS/test_add.py` 在守卫眼里不是 `tests` 目录，git 却老老实实把
    它写进了 `tests/test_add.py`，断言被删掉而守卫一声不吭。代价是一个同时
    存在 `tests/` 与 `TESTS/` 两个**不同**目录的仓库会被多拦一次 —— 这道守
    卫挡的是「模型删断言让测试变绿」，宁可多拦不可漏放；何况那样的仓库本身
    就是病态的。
    """
    p = tuple(seg.casefold() for seg in _path_parts(path))
    for d in dirs:
        prefix = tuple(seg.casefold() for seg in _path_parts(d))
        if prefix and p[:len(prefix)] == prefix:
            return True
    return False


@dataclass(frozen=True)
class PatchSignals:
    removed_public_symbols: list[str]
    new_module_state: list[str]
    files_outside_suspect: list[str]
    # 默认空列表：这个类被手工构造的地方（测试夹具、旧 checkpoint 的反序列化）
    # 不该因为多了一类信号就全部改签名。
    hardcoded_literals: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return (len(self.removed_public_symbols) + len(self.new_module_state)
                + len(self.files_outside_suspect)
                + len(self.hardcoded_literals))

    def is_empty(self) -> bool:
        return self.count == 0


def _parse(source: str | None) -> ast.Module | None:
    """解析失败一律当作「这一侧没有可分析的内容」。

    补丁把文件写坏了，测试自然会红、判定自然是 WORSE —— 信号模块跟着抛
    异常只会把一个已经被正确处理的失败升级成整轮崩溃。
    """
    if source is None:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def public_symbols(source: str) -> set[str]:
    """模块级 def/class，以及类里的方法（表示为 Class.method）。名字不以 _ 开头。

    刻意不看变量：__all__ 之外的模块级常量改名太常见，会把信号淹掉。
    """
    tree = _parse(source)
    if tree is None:
        return set()
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(node.name):
                found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if not _is_public(node.name):
                # 私有类整体不是公开面，它的方法也就谈不上「被删掉的公开
                # 符号」—— 逐个报出来只会淹掉真正的信号。
                continue
            found.add(node.name)
            found.update(f"{node.name}.{m.name}" for m in node.body
                         if isinstance(m, (ast.FunctionDef,
                                           ast.AsyncFunctionDef))
                         and _is_public(m.name))
    return found


def _is_mutable_value(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set,
                          ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in _MUTABLE_BUILTINS)


def module_state(source: str) -> set[str]:
    """模块级的可变赋值名：右值是 list/dict/set 字面量或推导式，
    或对 list/dict/set 的调用。

    模块级可变状态是「把纯函数改成有状态函数」最直接的指纹。
    """
    tree = _parse(source)
    if tree is None:
        return set()
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and _is_mutable_value(node.value):
                found.add(node.target.id)
        elif isinstance(node, ast.Assign) and _is_mutable_value(node.value):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return found


# 「到处都是」的字面量阈值：绝对值不超过它的整数/浮点数不算特征。
# 0 / 1 / 2 / -1 在实现和测试里都密集出现，算进来这一列会恒亮，而恒亮的信号
# 和没有这条信号是一个结果。
# **代价**：`if n == 0: return 1` 这种小数硬编码抓不到。精确率换召回率是刻意
# 的取舍 —— 读者是人，误报一次（下次直接无视这一节）比漏报一次贵。
_TRIVIAL_MAGNITUDE = 2

# 判断源码截断长度。整条判断原样放进报告会把那一节撑爆（模型写的复合条件能
# 有好几行），而人只需要认出是哪一处、再去 diff 里看全貌。
_CONDITION_MAX_CHARS = 60


def _is_distinctive(value: object) -> bool:
    """这个字面量够不够「特征」，值得拿去和测试里的对比。

    bool 必须排在 int 前面判：Python 里 `isinstance(True, int)` 为真，
    而 `True == 1`，于是 `{1} & {True}` 非空 —— 不先挡掉的话，任何一个新增
    的 `if flag:` 判断都可能被一个写了 `1` 的测试点亮。
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return abs(value) > _TRIVIAL_MAGNITUDE
    if isinstance(value, str):
        # 单字符（"a"、"/"、""）在两侧都太常见
        return len(value) >= 2
    return False


def distinctive_literals(source: str | None) -> set[object]:
    """目标测试文件里那些**有特征**的字面量。

    解析不了就返回空集合 —— Java / TypeScript 的测试文件走到这里是正常情形
    （`Failure.file` 三个适配器都会给），退成「这一类没有话说」，而不是报错。
    这一类因此是 **Python-only** 的，与本模块其余部分同一条约定。
    """
    tree = _parse(source)
    if tree is None:
        return set()
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and _is_distinctive(n.value)}


def _conditionals(source: str | None) -> list[tuple[str, frozenset]]:
    """源码里所有判断表达式：(判断的源码, 里面那些有特征的字面量)。

    `if` 与三元表达式都算：`return 42 if len(items) == 3 else …` 和写成
    if 语句是同一件事，只认其中一种等于留了一扇门。
    """
    tree = _parse(source)
    if tree is None:
        return []
    out: list[tuple[str, frozenset]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue
        consts = frozenset(n.value for n in ast.walk(node.test)
                           if isinstance(n, ast.Constant)
                           and _is_distinctive(n.value))
        out.append((ast.unparse(node.test), consts))
    return out


def _truncate(text: str) -> str:
    return (text if len(text) <= _CONDITION_MAX_CHARS
            else text[:_CONDITION_MAX_CHARS - 1] + "…")


def hardcoded_conditions(old: str | None, new: str | None,
                         literals: set[object]) -> set[str]:
    """补丁**新增**的判断里，用到了目标测试字面量的那些。

    这是 docs/safety.md「已知的天花板 §1」那个形状的指纹：

        def total(items):
            if len(items) == 3 and items[0].price == 10:   # 通过了所有测试
                return 42

    前三类信号一条都不亮，必要性反查也抓不到（这段硬编码**确实**让目标转绿，
    撤掉它目标就红 —— 按「有没有贡献」判它是必要的）。它是这四类里唯一直接
    对着规格套利的一条。

    **只报新增的**，与 `module_state` 同一条理由：旧版本里就有的判断不是这次
    补丁引入的，报出来等于给每个本来就有边界判断的函数贴一张永久红标签。
    用 Counter 做差而不是集合做差 —— 同一条判断被复制成两处是可疑的，集合差
    会把它整个吃掉。

    **不看新增判断的函数体**，只看判断本身。`return 42` 里的 42 同样来自测试，
    但把函数体算进来之后，「新增一条正常的边界判断、里面返回一个业务常量」也
    会亮 —— 而那是完全正常的代码。判断条件里出现测试的输入值，才是「照着这一
    条用例的输入开后门」的特征。
    """
    old_counts = Counter(text for text, _ in _conditionals(old))
    found: set[str] = set()
    for text, consts in _conditionals(new):
        if old_counts[text]:
            old_counts[text] -= 1
            continue
        if consts & literals:
            found.add(_truncate(text))
    return found


def analyze(files: dict[str, tuple[str | None, str | None]],
            suspect: str | None,
            suspect_anchored: bool = True,
            test_source: str | None = None) -> PatchSignals:
    """files: 路径 → (旧内容, 新内容)。None 表示该侧不存在（新增 / 删除）。

    suspect_anchored：Detector 手上是否有过**源码**栈帧。为 False 时
    `files_outside_suspect` 恒空 —— 理由与 suspect 为 None 时同一条，见下面
    那段注释：没有参照系就不比。

    已知偏差：`files_outside_suspect` 依赖 suspect 存在，而 suspect 来自
    Detector 的 JSON 诊断 —— detect_node 在 JSON 解析失败时把 diagnosis 置
    None，这里就没有参照系，这一列恒为空。于是**一个 JSON 输出不合规的模型，
    无论把改动摊到多少个文件，这一列都是 0**。不能靠伪造一个 suspect 来补：
    没有诊断就真的没有「之外」。读这一列时必须同时看
    `diagnosis_parse_failed`，跨模型对比的口径见 `eval/score.py` 的模块
    docstring。这与 `locate_hit` 曾经被「模型的路径书写风格」量走是同一类
    偏差，那次的教训写在 `eval/runner.locate_hit` 里。
    """
    removed: set[str] = set()
    new_state: set[str] = set()
    hardcoded: set[str] = set()
    # 没有目标测试的源码就没有参照系 —— 与 suspect 为 None 时同一条理由：
    # 编一个参照系只会让这一列变成假的。适配器给不出 `Failure.file`、测试文件
    # 不是 .py、或者文件读不到时，这一类整个不发声。
    literals = distinctive_literals(test_source)

    for path, (old, new) in files.items():
        if not path.endswith(_PY):
            continue
        if literals:
            hardcoded |= hardcoded_conditions(old, new, literals)
        old_symbols = public_symbols(old) if old is not None else set()
        new_symbols = public_symbols(new) if new is not None else set()
        removed |= old_symbols - new_symbols
        # 只报新增的：旧版本里就有的模块级可变状态不是这次补丁引入的，
        # 报出来等于给每个本来就有缓存的模块贴一张永久红标签。
        new_state |= (module_state(new) if new is not None else set()) - (
            module_state(old) if old is not None else set())

    # suspect 为 None 时没有「之外」可言：没有参照系就不比，否则这一列恒亮。
    #
    # suspect_anchored 为 False 是同一件事的另一种形状 —— Detector 有诊断，但
    # 那是在没有任何源码栈帧的情况下按包名猜的路径（纯断言失败的 traceback 里
    # 只有测试文件那一帧，这是最常见的一类失败）。猜 `src/cart.py` 而真文件是
    # `src/shopcart/cart.py` 时，一个修对了的补丁照样被标红。
    #
    # `old != new` 不能省：touched 只在 huge_diff 时整体清空，所以「打了补丁又
    # 打了反向补丁」的文件会与 HEAD 逐字相同却仍留在里面 —— 只看键会让人按图
    # 索骥去看一个空 diff。
    outside = ([p for p, (old, new) in files.items()
                if old != new and not same_file(p, suspect)]
               if suspect and suspect_anchored else [])

    # 三个列表都排序：报告与 facts 会消费它们，集合的迭代顺序不可复现，
    # 会让两次相同的运行产出不同的 diff。
    return PatchSignals(removed_public_symbols=sorted(removed),
                        new_module_state=sorted(new_state),
                        files_outside_suspect=sorted(outside),
                        hardcoded_literals=sorted(hardcoded))
