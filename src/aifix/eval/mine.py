"""从 git history 挖任务集。

规格 §9 的做法：
    找出让测试从红变绿的 commit C
    任务 = checkout 到 C^，但保留 C 中的测试文件
    期望 = agent 的补丁让该测试转绿且不引入回归
    对照 = C 中的源码改动即标准答案

自带 ground truth，分布真实 —— 不需要人来标注，也不会像人造变异那样
在分布上跑偏。
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath


def split_paths(paths: list[str],
                test_dirs: list[str]) -> tuple[list[str], list[str]]:
    """把 commit 改动的路径拆成（测试文件, 源文件）。"""
    tests: list[str] = []
    src: list[str] = []
    for p in paths:
        pp = PurePosixPath(p)
        if pp.suffix != ".py":
            continue
        # 目录判 + 文件名判：有的项目把测试和源码放在一起
        if (pp.parts and pp.parts[0] in test_dirs) or pp.name.startswith("test_"):
            tests.append(p)
        else:
            src.append(p)
    return tests, src


def is_candidate(test_files: list[str], gold_files: list[str]) -> bool:
    """同时动了测试与源码才可能是「红转绿」。

    只动测试 → 没有 gold；只动源码 → 没有判定用的 oracle。
    """
    return bool(test_files) and bool(gold_files)
