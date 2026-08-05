"""找「两处实现同一件事」的候选。

这是发现源里唯一一个不需要生产遥测、也不需要等真实用户踩到的：它自己造判据。
判据是**两处代码对同一输入给不同答案**，不需要任何人说出期望值。

判据用两个维度，缺一个都不成立：

  ① 分支在同一组字符串字面量上  —— 在处理同一个域
  ② 函数名有共同词根            —— 在做同一件事

只有 ① 时，「渲染题目」会和「判分」配成一对：它们确实共享题型枚举，但不是两处
实现同一件事。实测（ai-learning-helper 的 app/）只有 ① 是 18 对候选，两个维度
一起是 2 对，而其中一对正是那个真 bug。

**这一层只出候选，不下结论。** 候选要靠「跑一遍看结果一不一致」证伪，一致的
静默丢弃 —— 一个不能被便宜地证伪的候选就是噪音，而人学会无视一节报告只需要
两次。
"""
import textwrap

from aifix.discover.twins import branch_literals, find_twins, name_roots


def _fn(src: str):
    import ast
    return ast.parse(textwrap.dedent(src)).body[0]


# ---------------------------------------------------------- 分派字面量

def test_equality_comparisons_are_dispatch_keys():
    f = _fn('''
        def grade(q):
            if q["type"] == "single":
                return 1
            if q["type"] == "multiple":
                return 2
            return 0
    ''')
    assert branch_literals(f) == {"single", "multiple"}


def test_membership_over_a_tuple_counts():
    f = _fn('''
        def render(q):
            if q["type"] in ("single", "multiple"):
                return "choice"
    ''')
    assert branch_literals(f) == {"single", "multiple"}


def test_match_cases_count():
    f = _fn('''
        def go(kind):
            match kind:
                case "alpha":
                    return 1
                case "beta":
                    return 2
    ''')
    assert branch_literals(f) == {"alpha", "beta"}


def test_literals_that_are_not_dispatch_keys_are_skipped():
    """赋值、返回、拼接里的字面量不是分派依据 —— 收进来会淹掉信号。"""
    f = _fn('''
        def go(kind):
            label = "这是一个很长的说明文字"
            if kind == "alpha":
                return "结果 " + "后缀"
            return label
    ''')
    assert branch_literals(f) == {"alpha"}


def test_one_character_literals_are_not_dispatch_keys():
    """`x == ""` / `x == "y"` 到处都是，算进来这一列会恒亮。"""
    f = _fn('''
        def go(s):
            if s == "":
                return 0
            if s == "y":
                return 1
            if s == "yes":
                return 2
    ''')
    assert branch_literals(f) == {"yes"}


# ---------------------------------------------------------- 名字词根

def test_snake_case_is_split():
    assert name_roots("grade_objective") == {"grade", "objective"}


def test_camel_case_is_split_too():
    assert name_roots("gradeObjective") == {"grade", "objective"}


def test_leading_underscore_does_not_become_a_root():
    assert name_roots("_display_answer") == {"display", "answer"}


def test_generic_verbs_are_not_roots():
    """`get` / `run` / `handle` 到处都是，拿它们配对等于随机配。"""
    assert name_roots("get_thing") == {"thing"}
    assert name_roots("run") == set()


def test_short_tokens_are_dropped():
    assert name_roots("to_id") == set()


# ---------------------------------------------------------- 配对

_GRADER = '''
def grade_objective(q, parsed):
    typ = q["type"]
    if typ == "truefalse":
        return parsed is bool(q["answer"])
    if typ == "single":
        return parsed == q["answer"]
    if typ == "multiple":
        return sorted(set(parsed or [])) == sorted(set(q["answer"] or []))
    return False
'''

_QUIZ = '''
def grade(question, user_answer):
    t = question["type"]
    if t == "single":
        return user_answer == question["answer"]
    if t == "truefalse":
        return isinstance(user_answer, bool)
    if t == "multiple":
        return sorted(user_answer or []) == sorted(question["answer"])
    raise ValueError(t)
'''

# 共享同一组题型枚举，但做的是**渲染**不是判分 —— 只有维度 ① 成立。
_RENDER = '''
def present_question(q):
    typ = q["type"]
    if typ == "single":
        return "单选"
    if typ == "multiple":
        return "多选"
    if typ == "truefalse":
        return "判断"
    return "?"
'''


def _repo(tmp_path, **files):
    for name, src in files.items():
        (tmp_path / name).write_text(textwrap.dedent(src), encoding="utf-8")
    return tmp_path


def test_two_implementations_of_the_same_thing_are_paired(tmp_path):
    """这条是验收：ai-learning-helper#93 的形状必须被找出来。"""
    root = _repo(tmp_path, exam_grader=_GRADER, quiz_service=_QUIZ,
                 **{"exam_grader.py": _GRADER, "quiz_service.py": _QUIZ})
    twins = find_twins(root)
    assert len(twins) == 1
    t = twins[0]
    assert {t.a.name, t.b.name} == {"grade_objective", "grade"}
    assert "grade" in t.shared_roots
    assert {"single", "multiple", "truefalse"} <= t.shared_literals


def test_same_domain_but_different_job_is_not_a_pair(tmp_path):
    """只有维度 ① 时不算候选 —— 否则「渲染」会和「判分」配成一对。"""
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER,
                              "render.py": _RENDER})
    assert find_twins(root) == []


def test_two_functions_in_one_file_are_not_a_pair(tmp_path):
    """同一个文件里的两个函数不算「两处实现」—— 那多半是重载或分步。"""
    root = _repo(tmp_path, **{"both.py": _GRADER + "\n" + _QUIZ})
    assert find_twins(root) == []


def test_test_files_are_excluded(tmp_path):
    """测试文件里当然会有和产品代码同名同枚举的东西。"""
    (tmp_path / "tests").mkdir()
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER})
    (tmp_path / "tests" / "test_grade.py").write_text(
        textwrap.dedent(_QUIZ), encoding="utf-8")
    assert find_twins(root) == []


def test_a_syntax_error_does_not_stop_the_scan(tmp_path):
    """一个坏文件不该让整次扫描什么都不产出。"""
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER,
                              "quiz_service.py": _QUIZ,
                              "broken.py": "def f(:\n  pass\n"})
    assert len(find_twins(root)) == 1


def test_the_threshold_is_configurable(tmp_path):
    """分派键少于阈值就不算 —— 两个函数都判 `x == "a"` 不说明它们同域。"""
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER, "quiz_service.py": _QUIZ})
    assert find_twins(root, min_shared=99) == []


def test_candidates_carry_enough_to_locate_them(tmp_path):
    """报告与下一层都要能直接跳过去：路径 + 行号 + 名字。"""
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER, "quiz_service.py": _QUIZ})
    t = find_twins(root)[0]
    for site in (t.a, t.b):
        assert site.path.endswith(".py") and not site.path.startswith("/")
        assert site.lineno > 0 and site.name


def test_results_are_ordered_and_reproducible(tmp_path):
    """两次扫同一个仓库必须给出逐字相同的结果 —— 集合的迭代顺序不可复现。"""
    root = _repo(tmp_path, **{"exam_grader.py": _GRADER, "quiz_service.py": _QUIZ,
                              "other.py": _GRADER.replace("grade_objective",
                                                          "grade_second")})
    a = [(t.a.name, t.b.name) for t in find_twins(root)]
    b = [(t.a.name, t.b.name) for t in find_twins(root)]
    assert a == b and len(a) >= 2


# ---------------------------------------------------------- CLI

def test_scan_is_wired_into_the_parser():
    """加了 parser 却忘了接分派的话，命令会静默什么都不做。"""
    from aifix.cli import _dispatch, build_parser

    args = build_parser().parse_args(["scan", "--repo", "."])
    assert args.cmd == "scan"
    assert "scan" in _dispatch()


def test_scan_prints_candidates_and_says_they_are_not_conclusions(
        tmp_path, capsys):
    """输出必须写明这是候选、要靠跑一遍证伪 —— 不写的话它读起来就是
    一份 bug 清单，而这一层没有资格下那个结论。"""
    from aifix.cli import _cmd_scan

    (tmp_path / "exam_grader.py").write_text(_GRADER, encoding="utf-8")
    (tmp_path / "quiz_service.py").write_text(_QUIZ, encoding="utf-8")

    _cmd_scan(type("A", (), {"repo": str(tmp_path), "min_shared": 3,
                         "probe": False, "fix": False, "max_probes": 5})())
    out = capsys.readouterr().out
    assert "grade_objective" in out and "grade" in out
    assert "候选" in out
    assert "候选，不是结论" in out and "跑一遍" in out


def test_scan_says_so_when_it_finds_nothing(tmp_path, capsys):
    """空结果要出声。静默退出与「扫了但没扫到」区分不开。"""
    from aifix.cli import _cmd_scan

    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _cmd_scan(type("A", (), {"repo": str(tmp_path), "min_shared": 3,
                         "probe": False, "fix": False, "max_probes": 5})())
    assert "没有" in capsys.readouterr().out
