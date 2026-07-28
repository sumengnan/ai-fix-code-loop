from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

from .base import Failure, SourceCandidate

# 形如：  File "/abs/path/calc.py", line 2, in add
_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')

_MARKERS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py")


class PytestAdapter:
    name = "pytest"

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
        return [sys.executable, *self._BASE, f"--junitxml={self.REPORT_NAME}"]

    def scoped_test_command(self, test_ids: list[str]) -> list[str]:
        return [sys.executable, *self._BASE,
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

    def locate_source(self, failure: Failure, repo: Path) -> list[SourceCandidate]:
        """从 traceback 抽出 repo 内部帧，最深的排最前。"""
        repo_real = str(Path(repo).resolve())
        out: list[SourceCandidate] = []
        for m in _FRAME.finditer(failure.trace):
            raw = m.group("path")
            try:
                real = str(Path(raw).resolve())
            except OSError:
                continue
            if not (real == repo_real or real.startswith(repo_real + "/")):
                continue
            if "site-packages" in real or "/dist-packages/" in real:
                continue
            out.append(SourceCandidate(
                path=str(Path(real).relative_to(repo_real)),
                line=int(m.group("line")),
                frame=m.group("fn"),
            ))
        out.reverse()          # traceback 由浅入深，最深的最可疑
        return out
