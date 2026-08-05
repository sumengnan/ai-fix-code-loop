"""跨 run 轨迹：把散在各个 run 目录里的 facts.jsonl 灌进一张可查询的表。

单次 run 的分析 jsonl 撑得住 —— 一个目录、几十行、grep 就够。但「这个模型
最近十轮的定位准确率趋势」「哪一条守卫触发得最频繁」问的是**按 key 聚合、
按时间排序、按 run 关联**，那是 SQL 干的事；用 grep 拼出来的数字既不可信
也不可复用。

**不在 run 结束时自动落库**：那等于给核心循环加一条可能失败的写路径 ——
磁盘满、db 被别的进程锁住、schema 对不上，任何一个都会把「测试已经修好、
补丁已经提交到交付分支」的一次 run 变成一次失败。而这张表是**诊断用的**，
事后灌一次就够，晚几分钟没有任何代价。灌库因此是一个独立的、可以重来的
动作，这也是它必须幂等的原因。

数据契约：
- 一个 run 目录 = 一个 run_id，目录名是权威（facts 行里的 run_id 字段只是
  副本；目录里混进别的 run_id 时，按副本删会漏删，重灌就翻倍）。
- `facts.value` 一律是 **JSON 文本**：真实产物里 value 有字符串、数字、
  布尔，也有列表（三类信号的 value 就是列表）。一列里混着裸值和 JSON 之后
  没人能安全地解它。代价是查询时必须按 JSON 解 —— `WHERE value='better'`
  永远匹配不到，字符串在库里是 `"better"`（带引号），比大小更是无从谈起。
- 取不到的字段一律存 NULL，不填 0、不填空串。花了 token 却记 ¥0.00 这种假
  数字，比缺一列难查得多。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_RELPATH = Path(".aifix") / "trajectory.db"

# 与 eval/runner._SIGNAL_KEYS 是同一套 key，两份必须一致（tests 里钉了这
# 一条）。这里不 import 那一份：eval.runner 顶部 `from ...cli import run_once`，
# 而 cli 反过来要挂上本模块的子命令，import 就成环了。
SIGNAL_KEYS = frozenset({"removed_public_symbol", "new_module_state",
                         "files_outside_suspect", "hardcoded_literal"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT, adapter TEXT, branch TEXT,
    baseline_failures INTEGER, fixed INTEGER,
    spent_tokens INTEGER, spent_cny REAL,
    -- 换币种之前的老列。**保留而不是改写**：里面的数是美元，按当时某个不可
    -- 考的汇率折成人民币只会得到一列看着正常的假数字，而这张表的用途正是
    -- 「按成本排序、按成本汇总」。新 run 一律只写 spent_cny，老行两列并存，
    -- 按成本聚合时要么只取一列，要么自己决定怎么折。
    spent_usd REAL,
    abort TEXT, abort_kind TEXT
);
-- value 是 JSON 文本（见模块 docstring）：读的时候要 json.loads，
-- 不要直接比大小，也不要拿它和裸字符串相等比。
-- failure / attempt 只有 failure 级的事实才有；run 级的（baseline_failures、
-- abort…）存 NULL。
CREATE TABLE IF NOT EXISTS facts (
    run_id TEXT, failure TEXT, attempt INTEGER,
    key TEXT, value TEXT
);
CREATE INDEX IF NOT EXISTS facts_key ON facts(key);
"""

# 这五列的类型。run_once 各写一条同名的 fact，是它们的**数据契约**来源。
_COLUMN_TYPES: dict[str, Any] = {
    "adapter": str, "branch": str, "fixed": int,
    "spent_tokens": int, "spent_cny": (int, float), "spent_usd": (int, float),
}

# 下面这套正则只是**回退**：M4 之前落下的 run 没写过这五条 fact，只有渲染出来
# 的 report.md。用正则解自己渲染的 markdown 是一条会悄悄断掉的链子 —— 报告改
# 一个字，五列就静默变成 NULL，而聚合查询照跑不误。所以新产物一律走 fact，
# 这里只伺候已经落盘、再也不会重新生成的老目录。
# 每条都写成「解不出来就是 None」：报告改版时这几列变 NULL，而不是解出一个错的数。
_RE_ADAPTER = re.compile(r"^- 适配器：(.+)$", re.M)
_RE_BRANCH = re.compile(r"^- 分支：`(.+)`$", re.M)
_RE_FIXED = re.compile(r"^- 修复：\*\*(\d+) / (\d+)\*\*$", re.M)
# 成本那一行现在带汇率说明（`- 成本：¥1.42（12,345 tokens，按 1 USD = …）`），
# 所以 token 数后面允许跟一段任意文字。老报告没有那段，同一条正则都吃得下。
_RE_COST = re.compile(r"^- 成本：(.*)（([\d,]+) tokens[^（）]*）$", re.M)


def _read_facts(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读 facts.jsonl，坏行跳过。

    被 kill 的 run 会在末尾留下半行。为一行坏行让整个仓库的历史都灌不进去，
    等于把诊断工具变成第二个故障点 —— 而人来查这张表的时刻，往往正是刚出过
    一次这样的事故。
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("key"), str):
                yield rec


def _parse_report(path: Path) -> dict[str, Any]:
    """从 report.md 解出 facts.jsonl 里没有的那几个字段。"""
    out: dict[str, Any] = {"adapter": None, "branch": None, "fixed": None,
                           "spent_tokens": None, "spent_cny": None,
                           "spent_usd": None}
    if not path.is_file():
        return out
    text = path.read_text(encoding="utf-8")
    if m := _RE_ADAPTER.search(text):
        out["adapter"] = m.group(1).strip()
    if m := _RE_BRANCH.search(text):
        out["branch"] = m.group(1)
    if m := _RE_FIXED.search(text):
        out["fixed"] = int(m.group(1))
    if m := _RE_COST.search(text):
        out["spent_tokens"] = int(m.group(2).replace(",", ""))
        cost = m.group(1)
        # 没配价格表时这里是「未知（未配置 AIFIX_PRICE_MAP）」。解成 0.0 就
        # 是伪造 ¥0.00：往后所有按成本排序、按成本汇总的结论都是假的，而它
        # 看起来完全正常。
        #
        # 按符号分列，**不换算**：`$` 只出现在换币种之前的老报告里，那时
        # 用的汇率无从考证，折一个数出来就是在编。
        for sym, col in (("¥", "spent_cny"), ("$", "spent_usd")):
            if cost.startswith(sym):
                try:
                    out[col] = float(cost[1:])
                except ValueError:
                    pass
                break
    return out


def _run_columns(run_level: dict[str, Any],
                 report: dict[str, Any]) -> dict[str, Any]:
    """五列优先取 fact，取不到才回退到 report.md 解出来的那份。"""
    out: dict[str, Any] = {}
    for key, want in _COLUMN_TYPES.items():
        if key in run_level:
            value = run_level[key]
            # value 为 None 是**有意写下的**：花了 token 却没配价格表时，成本
            # 是「不知道」而不是 0。这也是一条取到了的事实，不能因为它是空的
            # 就退回去解报告 —— 报告那边同样只会解出「未知」。
            if value is None:
                out[key] = None
                continue
            # 类型对不上说明产物的形状变了：宁可退回正则，也不要往 INTEGER 列
            # 里塞一个字符串（sqlite 不拦，此后所有比大小的查询都是错的）
            if isinstance(value, want) and not isinstance(value, bool):
                out[key] = value
                continue
        out[key] = report[key]
    return out


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """给已经存在的库补上新列。

    `CREATE TABLE IF NOT EXISTS` 对已建好的表**什么都不做** —— 加一列之后不
    补这一步的话，老库上每一次 ingest 都会以「no such column: spent_cny」
    炸掉，而这张表恰恰是长期资产，重建一个空的等于把历史扔了。
    """
    have = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    for col, decl in (("spent_cny", "REAL"), ("spent_usd", "REAL")):
        if col not in have:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")


def _ingest_one(con: sqlite3.Connection, run_dir: Path) -> None:
    run_id = run_dir.name
    rows = []
    run_level: dict[str, Any] = {}
    for rec in _read_facts(run_dir / "facts.jsonl"):
        key = rec["key"]
        attempt = rec.get("attempt")
        rows.append((
            run_id,
            rec.get("failure"),
            attempt if isinstance(attempt, int) else None,
            key,
            # sort_keys：dict 型的 value（signals_discarded）两次灌库要得到
            # 逐字节相同的文本，否则按 value 分组会把同一条事实拆成两组
            json.dumps(rec.get("value"), ensure_ascii=False, sort_keys=True),
        ))
        if key in ("baseline_failures", "abort", "abort_kind") \
                or key in _COLUMN_TYPES:
            run_level[key] = rec.get("value")

    # 先删后插：run_id 不是 facts 的主键（一个 run 有几十条），INSERT OR
    # REPLACE 对它无能为力。少了这一句，重灌一次所有聚合数字就翻一倍 ——
    # 不报错、不崩溃，只是从此以后每个数都是错的。
    con.execute("DELETE FROM facts WHERE run_id = ?", (run_id,))
    con.executemany(
        "INSERT INTO facts(run_id, failure, attempt, key, value) "
        "VALUES (?, ?, ?, ?, ?)", rows)

    cols = _run_columns(run_level, _parse_report(run_dir / "report.md"))
    baseline = run_level.get("baseline_failures")
    con.execute(
        "INSERT OR REPLACE INTO runs(run_id, started_at, adapter, branch, "
        "baseline_failures, fixed, spent_tokens, spent_cny, spent_usd, "
        "abort, abort_kind)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, _started_at(run_dir), cols["adapter"], cols["branch"],
         baseline if isinstance(baseline, int) else None,
         cols["fixed"], cols["spent_tokens"], cols["spent_cny"],
         cols["spent_usd"],
         run_level.get("abort"), run_level.get("abort_kind")))


def _started_at(run_dir: Path) -> str:
    """产物目录的 mtime。

    facts.jsonl 的行里没有时间戳，事件流里的时间也不覆盖 run 的开头，所以这
    个值实际是**最后一次写入**的时刻，不是 run 的起点。用它排序没问题（同
    一个 run 的两端差不了多少），拿它算时长会错。
    """
    return datetime.fromtimestamp(
        run_dir.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def ingest(repo: Path | str, runs_dir: Path | str | None = None) -> int:
    """扫 `<repo>/.aifix/runs/*/facts.jsonl` 落库，返回**本次处理**的 run 数。

    `runs_dir` 覆盖扫描位置，库仍然落在 `<repo>/.aifix/trajectory.db`。它是为
    **Actions 上的产物离散**准备的：runner 是临时的，每次 run 各自消失，那个
    默认目录下永远只有一个 run，跨 run 汇总失去意义。把 `aifix/traces` 分支
    clone 下来指到这里，历史就重新连成一片（见 aifix.observe.traces）。

    幂等：同一批产物灌任意多次，表里的行数不变。因此返回值是处理数而不是
    新增数 —— 「这次多灌进去几个新 run」需要先查一次库才知道，而调用方真正
    关心的是「这个仓库里有几个 run 被覆盖到了」。

    返回 0 时**不产生任何磁盘副作用**：库不建、已有的库也不动。
    """
    repo = Path(repo)
    runs_dir = Path(runs_dir) if runs_dir else repo / ".aifix" / "runs"
    dirs = sorted(d for d in runs_dir.iterdir()
                  if (d / "facts.jsonl").is_file()) if runs_dir.is_dir() else []
    if not dirs:
        # 无事可灌就**不要建库**。`_connect` 会 mkdir + connect，空跑一次
        # 也足以把文件造出来，而 `aifix stats` 只认「db 文件在不在」：库一旦
        # 存在，「还没灌过库，先去 ingest」那句提示就永远不再出现，取而代之
        # 的是三个空小节 + 退出码 0 —— 正是 `_cmd_stats` 明写要避免的读法。
        # `--repo` 打错一次就够在那个错路径上永久制造这个假象。
        # 注意是「不碰」而不是「先删再看要不要建」：run 目录是可以随时清理的
        # 临时产物，这张表是长期资产，清掉产物重灌不该抹掉历史。
        return 0
    con = _connect(repo / DB_RELPATH)
    try:
        for d in dirs:
            _ingest_one(con, d)
        con.commit()
    finally:
        con.close()
    return len(dirs)


def _resolve_db(db_or_repo: Path | str) -> Path:
    """既接 db 文件，也接仓库根目录 —— 后者是人手里更常有的那个路径。"""
    p = Path(db_or_repo)
    return p if p.is_file() else p / DB_RELPATH


def query_stats(db_or_repo: Path | str) -> dict[str, Any]:
    """跨 run 的三张小结。

    - `by_adapter`：{adapter: {"runs": n, "fixed": m, "unknown": k}}。
      `fixed` 可能是 None —— 那一批 run 的报告一个都没解出修复数时，写 0 就
      是在说「一个都没修好」，那是另一回事。
      `unknown` 是这一组里**修复数取不到的行数**，它不是冗余：SQL 的 `sum`
      跳过 NULL，一组里混着「解得出」和「解不出」时会聚合出一个看着完全正
      常的数（1 次修好 2 个 + 2 次不知道 → `sum` 给 2），而单看这个数没有
      任何办法判断它是不是完整的。渲染侧据此标注。
    - `guard_hits`：[(种类, 次数)]，按次数降序。
    - `signal_runs`：[(run_id, 可疑信号条数)]，按条数降序。
    """
    db = _resolve_db(db_or_repo)
    empty: dict[str, Any] = {"by_adapter": {}, "guard_hits": [],
                             "signal_runs": []}
    if not db.is_file():
        # 读不该有副作用：没灌过库就说没有，不要顺手建一个空库出来
        return empty
    con = sqlite3.connect(db)
    try:
        # `sum(fixed IS NULL)` 与 `sum(fixed)` 必须一起取：前者是后者的完整
        # 性凭据。只取后者的话，「取不到」被 sum 跳过，产出的合计与「这几次
        # 都修了 0 个」逐字节相同。
        by_adapter = {
            adapter: {"runs": n, "fixed": fixed, "unknown": unknown}
            for adapter, n, fixed, unknown in con.execute(
                "SELECT adapter, count(*), sum(fixed), sum(fixed IS NULL) "
                "FROM runs GROUP BY adapter ORDER BY count(*) DESC, adapter")}
        # 按 value 的 **JSON 文本** 分组，解 JSON 放到 Python 里做：标量的
        # JSON 编码是唯一的（json.dumps("empty_diff") 恒为 '"empty_diff"'），
        # 所以按文本分组与按值分组等价，却不依赖 SQLite 是否编进了 json1。
        guard_hits = [(_decode(v), n) for v, n in con.execute(
            "SELECT value, count(*) FROM facts WHERE key = 'guard_hit' "
            "GROUP BY value ORDER BY count(*) DESC, value")]
        keys = sorted(SIGNAL_KEYS)
        signal_runs = con.execute(
            "SELECT run_id, count(*) FROM facts "
            f"WHERE key IN ({','.join('?' * len(keys))}) "
            "GROUP BY run_id ORDER BY count(*) DESC, run_id", keys).fetchall()
    finally:
        con.close()
    return {"by_adapter": by_adapter, "guard_hits": guard_hits,
            "signal_runs": signal_runs}


def _decode(value: str) -> Any:
    """解不开就原样返回：分组用的字面量不该因为一条脏数据而丢失。"""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
