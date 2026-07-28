from aifix.eval.mine import is_candidate, split_paths

_DIRS = ["tests", "test"]


def test_splits_tests_from_source():
    tests, src = split_paths(["tests/test_calc.py", "calc.py"], _DIRS)
    assert tests == ["tests/test_calc.py"]
    assert src == ["calc.py"]


def test_test_prefixed_file_outside_test_dir_counts_as_test():
    """有的项目把测试和源码放一起 —— 按目录判会漏。"""
    tests, src = split_paths(["pkg/test_util.py", "pkg/util.py"], _DIRS)
    assert tests == ["pkg/test_util.py"]
    assert src == ["pkg/util.py"]


def test_non_python_files_are_dropped():
    tests, src = split_paths(["README.md", "calc.py", "data.json"], _DIRS)
    assert tests == []
    assert src == ["calc.py"]


def test_candidate_needs_both_sides():
    assert is_candidate(["tests/t.py"], ["a.py"]) is True
    assert is_candidate([], ["a.py"]) is False       # 没动测试 → 没有 oracle
    assert is_candidate(["tests/t.py"], []) is False  # 没动源码 → 没有 gold


def test_empty_commit_is_not_a_candidate():
    assert is_candidate(*split_paths([], _DIRS)) is False
