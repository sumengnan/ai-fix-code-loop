"""跨 run 轨迹库的验收：真产物进去，可聚合的表出来。

除标了「构造目录」的那几条用例外，所有 `.aifix/runs/*` 都是脚本化假客户端
**真跑** `run_once` 落下来的产物。手写 facts.jsonl 只能证明我们对自己的
理解自洽 —— 它对不上真实产物时，测试全绿而功能是坏的。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from harness.llm.base import StreamChunk, ToolCallDelta
from harness.usage import Usage

from aifix.observe import trajectory
from aifix.cli import run_once
from aifix.config import AifixConfig

# calc.py 里多一个 mul：让「补丁删掉公开符号」这条信号有机会真的被触发，
# 而不是靠手写一条 removed_public_symbol 的 fact 假装它发生过。
_BUGGY = '''def add(a, b):
    return a - b        # bug: 应为 a + b


def mul(a, b):
    return a * b
'''

_TEST = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_identity():
    assert add(0, 0) == 0
'''

# 修好 add 的同时删掉无测试覆盖的 mul —— verify 判 BETTER，
# 于是 removed_public_symbol 这条 fact 才会写进 facts.jsonl。
_PATCH_DROPS_MUL = """--- a/calc.py
+++ b/calc.py
@@ -1,6 +1,2 @@
 def add(a, b):
-    return a - b        # bug: 应为 a + b
-
-
-def mul(a, b):
-    return a * b
+    return a + b
"""

_DIAG = json.dumps({
    "suspect_file": "calc.py", "suspect_lines": [1, 2],
    "root_cause": "减号应为加号", "fix_strategy": "改回 a + b",
    "confidence": "high",
})


def _text(t):
    return [StreamChunk(type="text", text=t),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


def _tool(name, args):
    return [StreamChunk(type="tool_call", tool_call_delta=ToolCallDelta(
                index=0, id="c1", name=name, arguments=args)),
            StreamChunk(type="done", usage=Usage(10, 5, 15))]


class _Scripted:
    def __init__(self, turns):
        self._turns, self._i = list(turns), 0

    async def stream(self, messages, tools):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for c in turn:
            yield c


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    repo = root / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


async def _three_runs(repo: Path) -> None:
    """三次形状不同的真 run：修好并留下信号 / 撞空补丁守卫 / 撞超大补丁守卫。"""
    await run_once(repo, AifixConfig(), run_id="r_fix",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch",
                             json.dumps({"diff": _PATCH_DROPS_MUL})),
                       _text("已修复")]))
    # 模型一字未改 → empty_diff 守卫；默认配置下每个 attempt 撞 2 次、共 3 轮
    await run_once(repo, AifixConfig(), run_id="r_guard",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([_text("我什么都不做")]))
    # 把上限压到 1 行，同一个补丁改撞 huge_diff；只给 1 个 attempt，
    # 让两种守卫的次数**不相等** —— 相等的话排序断言恒真
    await run_once(repo, AifixConfig(max_diff_lines=1, max_attempts=1),
                   run_id="r_huge",
                   detector_client=_Scripted([_text(_DIAG)]),
                   fixer_client=_Scripted([
                       _tool("apply_patch",
                             json.dumps({"diff": _PATCH_DROPS_MUL})),
                       _text("已修复")]))


@pytest.fixture(scope="module")
def real_runs(tmp_path_factory) -> Path:
    """真跑三次 run_once，产出真实的 `.aifix/runs/*`（整个模块只跑一次）。"""
    repo = _make_repo(tmp_path_factory.mktemp("traj"))
    asyncio.run(_three_runs(repo))
    return repo / ".aifix" / "runs"


@pytest.fixture
def repo(tmp_path: Path, real_runs: Path) -> Path:
    """每个用例一份独立仓库：db 落在仓库里，共用会让用例之间隔空传状态。"""
    dst = tmp_path / "repo"
    (dst / ".aifix").mkdir(parents=True)
    shutil.copytree(real_runs, dst / ".aifix" / "runs")
    return dst


def _db(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".aifix" / "trajectory.db")


def _one(repo: Path, sql: str, *args):
    with _db(repo) as con:
        return con.execute(sql, args).fetchone()[0]


# ---------- 核心：幂等 ----------

def test_同一批_run_灌两次_facts_行数不变(repo: Path) -> None:
    """第二次灌进去翻倍不报错、不崩溃，只让此后所有聚合数字翻倍。"""
    assert trajectory.ingest(repo) == 3
    first_facts = _one(repo, "SELECT count(*) FROM facts")
    first_runs = _one(repo, "SELECT count(*) FROM runs")
    # 真产物里 facts 有几十条；为 0 的话下面「不变」是恒真的
    assert first_facts > 10

    # 返回值是**本次处理**的 run 数，重复灌仍是 3（不是新增数）
    assert trajectory.ingest(repo) == 3
    assert _one(repo, "SELECT count(*) FROM facts") == first_facts
    assert _one(repo, "SELECT count(*) FROM runs") == first_runs == 3


def test_重灌只影响被重灌的_run(repo: Path) -> None:
    """先删后插的删除范围必须限定在这个 run_id 上，不能把别的 run 一起抹掉。"""
    trajectory.ingest(repo)
    before = _one(repo, "SELECT count(*) FROM facts WHERE run_id='r_guard'")
    assert before > 0

    # 只留一个 run 目录再灌一次：另外两个 run 的行必须原样还在
    shutil.rmtree(repo / ".aifix" / "runs" / "r_fix")
    shutil.rmtree(repo / ".aifix" / "runs" / "r_huge")
    assert trajectory.ingest(repo) == 1
    assert _one(repo, "SELECT count(*) FROM facts WHERE run_id='r_guard'") \
        == before
    assert _one(repo, "SELECT count(*) FROM runs") == 3


# ---------- 真产物里的字段 ----------

def test_run_级事实的_failure_与_attempt_是_NULL_不是空串(repo: Path) -> None:
    """SQL 里 NULL 和空串不是一回事，聚合时会咬人。"""
    trajectory.ingest(repo)
    with _db(repo) as con:
        assert con.execute(
            "SELECT failure, attempt FROM facts "
            "WHERE run_id='r_fix' AND key='baseline_failures'").fetchall() \
            == [(None, None)]
        # 对照组：failure 级的事实必须真的带着 failure / attempt，
        # 否则上面那条断言在「所有列全空」的实现下也是绿的
        assert con.execute(
            "SELECT failure, attempt FROM facts "
            "WHERE run_id='r_fix' AND key='verdict'").fetchall() \
            == [("tests/test_calc.py::test_add", 1)]
        assert con.execute(
            "SELECT count(*) FROM facts WHERE failure = ''").fetchone()[0] == 0


def test_value_一律存成_JSON_文本(repo: Path) -> None:
    trajectory.ingest(repo)
    with _db(repo) as con:
        touched = con.execute(
            "SELECT value FROM facts "
            "WHERE run_id='r_fix' AND key='touched'").fetchone()[0]
        # str(list) 会得到 "['calc.py']"，单引号解不回来
        assert touched == '["calc.py"]'
        assert json.loads(touched) == ["calc.py"]
        # 标量也走 JSON：字符串带引号、布尔是 true 不是 True
        assert con.execute(
            "SELECT value FROM facts "
            "WHERE run_id='r_fix' AND key='verdict'").fetchone()[0] == '"better"'
        assert json.loads(con.execute(
            "SELECT value FROM facts "
            "WHERE run_id='r_guard' AND key='rollback'").fetchone()[0]) is True


def test_没配价格表时_spent_cny_存_NULL_而不是零(repo: Path) -> None:
    """花了 token 却记 ¥0.00 是这个项目栽过两次的假数字。"""
    trajectory.ingest(repo)
    with _db(repo) as con:
        tokens, cny = con.execute(
            "SELECT spent_tokens, spent_cny FROM runs "
            "WHERE run_id='r_fix'").fetchone()
    assert tokens == 45          # 3 次假调用 × 15 token
    assert cny is None


def test_runs_行的字段取自真实产物(repo: Path) -> None:
    trajectory.ingest(repo)
    with _db(repo) as con:
        con.row_factory = sqlite3.Row
        fix = con.execute(
            "SELECT * FROM runs WHERE run_id='r_fix'").fetchone()
        guard = con.execute(
            "SELECT * FROM runs WHERE run_id='r_guard'").fetchone()
    assert fix["adapter"] == "pytest"
    assert fix["branch"] == "aifix/r_fix"
    assert fix["baseline_failures"] == 1
    assert fix["fixed"] == 1
    assert fix["abort"] is None and fix["abort_kind"] is None
    # 同一批产物里没修好的那个必须是 0 —— 不加这条，一个「fixed 恒等于
    # baseline_failures」的实现也能让上面全绿
    assert guard["fixed"] == 0
    assert fix["started_at"] and fix["started_at"] > "2020"


# ---------- 聚合 ----------

def test_五列各有一条_fact(repo: Path) -> None:
    """adapter / branch / fixed / 花销必须是落盘的事实，不是报告里的渲染结果。"""
    trajectory.ingest(repo)
    with _db(repo) as con:
        keys = {r[0] for r in con.execute(
            "SELECT key FROM facts WHERE run_id='r_fix'")}
    assert {"adapter", "branch", "fixed", "spent_tokens", "spent_cny"} <= keys


def test_五列取自_fact_而不是解_report_md(repo: Path) -> None:
    """把 report.md 全删掉，五列仍然正确。

    report.md 是**渲染**不是数据契约。只断言「值对」的话走正则也能全绿 ——
    区分度全在这一步删除上：报告改一个字，正则那条链子就静默断掉，五列变成
    NULL 而聚合查询不会报错。
    """
    for d in (repo / ".aifix" / "runs").iterdir():
        (d / "report.md").unlink()

    trajectory.ingest(repo)
    with _db(repo) as con:
        con.row_factory = sqlite3.Row
        fix = con.execute(
            "SELECT * FROM runs WHERE run_id='r_fix'").fetchone()
        guard = con.execute(
            "SELECT * FROM runs WHERE run_id='r_guard'").fetchone()
    assert fix["adapter"] == "pytest"
    assert fix["branch"] == "aifix/r_fix"
    assert fix["fixed"] == 1
    assert fix["spent_tokens"] == 45
    # 没配价格表 —— 这一列必须是 NULL，不能因为改走 fact 就变回假的 0.0
    assert fix["spent_cny"] is None
    # 对照组：没修好的那个是 0，不是 1（fixed 恒等于 baseline 的实现也会全绿）
    assert guard["fixed"] == 0


def test_配了价格表时_spent_cny_是真金额(tmp_path: Path) -> None:
    """配了价格表就必须落下真实金额 —— 而且同样不靠 report.md。

    没有这一条，一个「spent_cny 永远写 None」的实现也能让上面几条全绿：
    那批产物恰好都没配价格表，None 是对的答案。
    """
    repo = _make_repo(tmp_path / "priced")
    asyncio.run(run_once(
        repo, AifixConfig(price_map={"gpt-4o-mini": [1.0, 1.0]}),
        run_id="r_priced",
        detector_client=_Scripted([_text(_DIAG)]),
        fixer_client=_Scripted([
            _tool("apply_patch", json.dumps({"diff": _PATCH_DROPS_MUL})),
            _text("已修复")])))
    (repo / ".aifix" / "runs" / "r_priced" / "report.md").unlink()

    assert trajectory.ingest(repo) == 1
    with _db(repo) as con:
        tokens, cny = con.execute(
            "SELECT spent_tokens, spent_cny FROM runs "
            "WHERE run_id='r_priced'").fetchone()
    assert tokens == 45
    # 每次假调用 10 输入 + 5 输出、每 1k 各 $1.0 = $0.015，三次共 $0.045；
    # 价表是美元，落库前按默认汇率折成人民币
    assert cny == pytest.approx(0.045 * AifixConfig().usd_to_cny)


def test_没有_fact_的老产物回退解_report_md(tmp_path: Path) -> None:
    """构造目录：M4 之前落下的 run 只有 markdown，正则那条回退必须保留。"""
    d = tmp_path / ".aifix" / "runs" / "old"
    d.mkdir(parents=True)
    (d / "facts.jsonl").write_text(
        json.dumps({"run_id": "old", "key": "baseline_failures", "value": 2})
        + "\n", encoding="utf-8")
    (d / "report.md").write_text(
        "# aifix run old\n\n"
        "- 适配器：pytest\n"
        "- 分支：`aifix/old`\n"
        "- 修复：**1 / 2**\n"
        "- 成本：$0.1234（1,200 tokens）\n", encoding="utf-8")

    assert trajectory.ingest(tmp_path) == 1
    with _db(tmp_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM runs WHERE run_id='old'").fetchone()
    assert row["adapter"] == "pytest"
    assert row["branch"] == "aifix/old"
    assert row["fixed"] == 1
    assert row["spent_tokens"] == 1200
    # 老报告写的是美元，落进老列、**不折算** —— 当时用的汇率无从考证
    assert row["spent_usd"] == 0.1234
    assert row["spent_cny"] is None


def test_query_stats_按_adapter_聚合_run_数与修复数(repo: Path) -> None:
    trajectory.ingest(repo)
    stats = trajectory.query_stats(repo)
    # unknown=0：这三个 run 的修复数都解得出来，合计是完整的
    assert stats["by_adapter"] == {
        "pytest": {"runs": 3, "fixed": 1, "unknown": 0}}


def test_query_stats_单独报告取不到修复数的行数(repo: Path) -> None:
    """`sum(fixed)` 跳过 NULL，光看合计分不出「修了 1 个」和「修了 1 个 +
    2 次不知道」。unknown 是这个合计的完整性凭据，必须单独给出来。
    """
    for name in ("r_x", "r_y"):
        d = repo / ".aifix" / "runs" / name
        d.mkdir()
        (d / "facts.jsonl").write_text(
            json.dumps({"run_id": name, "key": "adapter", "value": "pytest"})
            + "\n", encoding="utf-8")
    trajectory.ingest(repo)

    row = trajectory.query_stats(repo)["by_adapter"]["pytest"]

    assert row == {"runs": 5, "fixed": 1, "unknown": 2}


def test_query_stats_守卫触发次数按种类降序(repo: Path) -> None:
    trajectory.ingest(repo)
    hits = trajectory.query_stats(repo)["guard_hits"]
    # 数字来自真产物：r_guard 每个 attempt 撞 2 次 empty_diff、共 3 个 attempt
    # = 6；r_huge 第一轮撞 huge_diff 被回滚，脚本化模型后两轮只说话不改文件，
    # 于是又补上 2 次 empty_diff（8 = 6 + 2，1 = huge_diff）。
    # 种类名必须是解过 JSON 的 empty_diff，不是带引号的 '"empty_diff"'
    assert hits == [("empty_diff", 8), ("huge_diff", 1)]


def test_query_stats_可疑信号最多的_run(repo: Path) -> None:
    trajectory.ingest(repo)
    top = trajectory.query_stats(repo)["signal_runs"]
    # 只有 r_fix 判了 BETTER 并删掉了 mul；撞守卫的两个 run 一条信号也没有，
    # 不能出现在这张榜上（出现即说明把被回滚的补丁也算进去了）
    assert top == [("r_fix", 1)]


def test_信号_key_与_eval_侧的定义不漂移() -> None:
    """两份各自漂移的话，同一批 facts 在两处会得出不同的信号数。"""
    from aifix.eval.runner import _SIGNAL_KEYS

    assert trajectory.SIGNAL_KEYS == _SIGNAL_KEYS


def test_facts_key_建了索引(repo: Path) -> None:
    trajectory.ingest(repo)
    with _db(repo) as con:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "facts_key" in names


# ---------- 构造目录（不依赖真产物的两条） ----------

def test_坏行不拖累同一个仓库的其它_run(tmp_path: Path) -> None:
    """构造目录：真产物不会半行截断，但被 kill 的 run 会。

    一行坏行让整仓历史都灌不进去，是把诊断工具变成第二个故障点。
    """
    runs = tmp_path / ".aifix" / "runs"
    (runs / "ok").mkdir(parents=True)
    (runs / "broken").mkdir(parents=True)
    (runs / "ok" / "facts.jsonl").write_text(
        '{"run_id": "ok", "key": "verdict", "value": "better"}\n',
        encoding="utf-8")
    (runs / "broken" / "facts.jsonl").write_text(
        '{"run_id": "broken", "key": "verdict", "value": "same"}\n'
        '{"run_id": "broken", "key": "diff_l\n',
        encoding="utf-8")

    assert trajectory.ingest(tmp_path) == 2
    with _db(tmp_path) as con:
        assert con.execute(
            "SELECT run_id, key FROM facts ORDER BY run_id").fetchall() \
            == [("broken", "verdict"), ("ok", "verdict")]


def test_没有产物目录时灌库不报错(tmp_path: Path) -> None:
    """构造目录：新仓库还没跑过 run。"""
    assert trajectory.ingest(tmp_path) == 0
    assert trajectory.query_stats(tmp_path) == {
        "by_adapter": {}, "guard_hits": [], "signal_runs": []}


def test_无事可灌时不建库(tmp_path: Path) -> None:
    """没有可灌的 run 就不该留下一个空 db。

    留下空 db 会把 `aifix stats` 从「还没灌过库，先去 ingest」翻成三个空
    小节 + 退出码 0 —— 而那正是 `_cmd_stats` 明写要避免的读法（「渲染一张
    空表会被读成『这个仓库没跑过 run』」）。`--repo` 打错一次就会在错路径
    上永久制造这个假象。
    """
    db = tmp_path / trajectory.DB_RELPATH

    # 目录完全空：连 .aifix/ 都不该建出来
    assert trajectory.ingest(tmp_path) == 0
    assert not db.exists()

    # runs/ 在、但里面没有一个带 facts.jsonl 的目录（半路被删/建了一半）
    (tmp_path / ".aifix" / "runs" / "空壳").mkdir(parents=True)
    assert trajectory.ingest(tmp_path) == 0
    assert not db.exists()

    # 区分度：真有可灌的 run 时库必须建出来，否则上面两条恒真
    (tmp_path / ".aifix" / "runs" / "r1").mkdir()
    (tmp_path / ".aifix" / "runs" / "r1" / "facts.jsonl").write_text(
        '{"run_id": "r1", "key": "verdict", "value": "better"}\n',
        encoding="utf-8")
    assert trajectory.ingest(tmp_path) == 1
    assert db.is_file()


def test_无事可灌时不删已有的库(tmp_path: Path) -> None:
    """产物目录被清掉之后重灌，不该把攒下来的历史一起抹掉。

    「不建库」必须是「不碰库」，不能顺手实现成「删掉再看要不要建」——
    run 目录是随时可以清理的临时产物，这张表才是长期资产。
    """
    runs = tmp_path / ".aifix" / "runs"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "facts.jsonl").write_text(
        '{"run_id": "r1", "key": "verdict", "value": "better"}\n',
        encoding="utf-8")
    assert trajectory.ingest(tmp_path) == 1

    shutil.rmtree(runs)
    assert trajectory.ingest(tmp_path) == 0
    with _db(tmp_path) as con:
        assert con.execute(
            "SELECT run_id FROM facts").fetchall() == [("r1",)]


def test_老库能被灌进去_而不是撞上缺列(tmp_path: Path) -> None:
    """换币种加了一列，而 `CREATE TABLE IF NOT EXISTS` 对已建好的表什么都不做。

    不补 ALTER 的话，老库上每一次 ingest 都以「no such column: spent_cny」
    炸掉 —— 而这张表是长期资产，重建一个空的等于把历史扔了。这条用例造的
    正是换币种之前那张表：只有 spent_usd，没有 spent_cny。
    """
    db = tmp_path / ".aifix" / "trajectory.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, started_at TEXT, adapter TEXT, branch TEXT,
        baseline_failures INTEGER, fixed INTEGER,
        spent_tokens INTEGER, spent_usd REAL, abort TEXT, abort_kind TEXT);
    CREATE TABLE facts (run_id TEXT, failure TEXT, attempt INTEGER,
                        key TEXT, value TEXT);
    """)
    con.execute("INSERT INTO runs(run_id, adapter, spent_usd) "
                "VALUES ('ancient', 'pytest', 0.42)")
    con.commit()
    con.close()

    d = tmp_path / ".aifix" / "runs" / "new"
    d.mkdir(parents=True)
    d.joinpath("facts.jsonl").write_text(
        json.dumps({"run_id": "new", "key": "spent_cny", "value": 3.5}) + "\n",
        encoding="utf-8")

    assert trajectory.ingest(tmp_path) == 1
    with _db(tmp_path) as con:
        con.row_factory = sqlite3.Row
        rows = {r["run_id"]: r for r in con.execute("SELECT * FROM runs")}
    assert rows["new"]["spent_cny"] == pytest.approx(3.5)
    # 老行原样留着：那是美元，按一个不可考的汇率折出来只会得到一个假数字
    assert rows["ancient"]["spent_usd"] == pytest.approx(0.42)
    assert rows["ancient"]["spent_cny"] is None
