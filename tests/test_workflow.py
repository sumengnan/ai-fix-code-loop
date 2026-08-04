"""把 workflow 里那几条**安全控制**钉住。

它们看起来只是 YAML 里的一行条件，但松掉任意一条的后果都不是「行为变了」，
而是「一条本不该被信任的输入进了模型上下文」。而 workflow 改动不会被任何单元
测试覆盖到 —— 除了这一份。
"""
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "aifix.yml"
_RAW = _PATH.read_text(encoding="utf-8")
_DOC = yaml.safe_load(_RAW)


def _on() -> dict:
    """YAML 1.1 把裸 `on` 解析成布尔 True（挪威问题）。GitHub 自己的解析器
    不这样，但 PyYAML 会 —— 按 True 取，别按 'on' 取。"""
    return _DOC[True] if True in _DOC else _DOC["on"]


def _job() -> dict:
    return _DOC["jobs"]["fix"]


def test_only_created_comments_trigger():
    """接 edited 的话，一条三个月前的旧评论被编辑成 /aifix 就能触发，
    而编辑不产生新通知，没人会注意到。"""
    assert _on()["issue_comment"]["types"] == ["created"]


def test_only_newly_opened_issues_trigger():
    """同一条理由，而 issue 正文更甚：改自己的 issue 正文是**完全静默**的 ——
    连一个 edited 通知都不会发出去。"""
    assert _on()["issues"]["types"] == ["opened"]


def test_both_entrances_require_the_command_prefix():
    """前置过滤只做一件事：把不带命令前缀的事件挡在起 job 之前。
    两条入口都要判，漏一条那一侧就会对每个 issue / 每条评论起一次 job。"""
    cond = _job()["if"]
    assert "startsWith(github.event.issue.body, '/aifix')" in cond
    assert "startsWith(github.event.comment.body, '/aifix')" in cond


def test_the_prefilter_does_not_decide_permissions():
    """**这一条是有意的，不是遗漏。**

    没权限的人打了 /aifix，产品要求是回帖告诉他 —— 而 job 的 `if:` 拦下来的
    事件根本不会起 job，也就没有任何东西能发出那条回帖。附带的第二个理由：
    `AIFIX_ALLOWED_USERS` 里的人 author_association 可能是 NONE，在这里判权限
    会让那份白名单彻底失效。

    权限判定的**唯一**去处是 aifix.issue.event.authorize（零 LLM，可脱网穷举）。
    这条测试防的是「顺手加一条 association 判据省点 runner 分钟数」——
    那会静默掐掉上面两件事。
    """
    cond = _job()["if"]
    assert "author_association" not in cond
    assert "repository_owner" not in cond


def test_the_allowlist_reaches_the_job_as_a_variable():
    """白名单在代码里是 `authorize(allowed_users=...)` 的参数，而参数由
    AifixConfig 从环境读。这条线断在 YAML 这一段的话，配了 variable 的人
    会发现名单**一声不吭地不起作用**。
    """
    env = _job()["steps"][-2]["env"]
    assert env["AIFIX_ALLOWED_USERS"] == "${{ vars.AIFIX_ALLOWED_USERS }}"


def test_permissions_are_written_out_explicitly():
    """不写的话仓库默认可能是宽松的，而「我以为它是最小权限」是这类事故最
    常见的开头。"""
    perms = _job()["permissions"]
    assert set(perms) == {"contents", "issues", "pull-requests"}
    assert "actions" not in perms and "packages" not in perms


def test_the_soft_wall_clock_gate_fires_before_the_hard_kill():
    """Actions 的超时是**杀进程**：run_once 里那个「保证报告先落地」的 except
    根本执行不到，跑了一小时什么都留不下。aifix 自己的墙钟闸必须先响。
    """
    step = next(s for s in _job()["steps"] if "aifix issue handle" in s.get("run", ""))
    soft = float(step["env"]["AIFIX_BUDGET_WALL_SECONDS"])
    hard = float(_job()["timeout-minutes"]) * 60
    assert soft < hard, f"软闸 {soft}s 不小于硬杀 {hard}s"


def test_the_artifact_upload_runs_even_on_failure():
    """崩了才最需要它。失败时不上传，等于恰好在最需要诊断数据的那一次把它扔了。"""
    up = next(s for s in _job()["steps"]
              if "upload-artifact" in str(s.get("uses", "")))
    assert up["if"] == "always()"


def test_the_test_interpreter_is_explicit_not_probed():
    """runner 上 clone 出来没有 .venv，自动探测落空会退回 aifix 自己的解释器，
    然后是一整批 collection error —— 这个坑这个项目已经踩过一次。"""
    step = next(s for s in _job()["steps"] if "aifix issue handle" in s.get("run", ""))
    assert step["env"]["AIFIX_TEST_PYTHON"]


def test_the_price_map_is_a_variable_not_a_secret():
    """放进 secret 会被日志遮蔽成 ***，你反而看不出它配没配对 —— 而没配价格表
    的后果是成本闸永远不触发。这个项目为「假的 0.00」栽过三次。"""
    assert "vars.AIFIX_PRICE_MAP" in _RAW
    assert "secrets.AIFIX_PRICE_MAP" not in _RAW


def test_checkout_is_not_shallow():
    """交付分支是从 HEAD 长出来的新分支，从浅克隆推新分支会被部分场景拒掉。"""
    co = next(s for s in _job()["steps"] if "actions/checkout" in str(s.get("uses", "")))
    assert co["with"]["fetch-depth"] == 0


def test_concurrency_is_scoped_per_issue():
    """手滑连点两次 = 两倍开销，而且两个 run 会抢同一个 worktree 路径。"""
    assert "github.event.issue.number" in _DOC["concurrency"]["group"]


def test_both_model_routes_are_configurable_without_editing_this_file():
    """换模型不该需要改 workflow。

    `issue_comment` 的 workflow 只从**默认分支**加载，所以改这个文件要走一次
    提交、评审、合并才生效 —— 那个摩擦足以让人干脆不换模型，于是「诊断用便宜
    的、修复用强的」这条设计在实践中就名存实亡了。

    两条路由**各自独立**：它们本来就可以是两个供应商。
    """
    step = next(s for s in _job()["steps"] if "aifix issue handle" in s.get("run", ""))
    for key in ("AIFIX_FIXER__MODEL", "AIFIX_DETECTOR__MODEL"):
        expr = step["env"][key]
        assert f"vars.{key}" in expr, f"{key} 没接仓库 variable"
        assert "||" in expr, f"{key} 没有缺省值，没配就会跑一个空模型名"

    # 反向对照：两条不能指向同一个 variable —— 那样就没法分开配了
    assert step["env"]["AIFIX_FIXER__MODEL"] != step["env"]["AIFIX_DETECTOR__MODEL"]


def test_model_names_are_variables_not_secrets():
    """与价格表同一条理由：secret 在日志里被遮成 ***，跑错模型时你从日志里
    根本看不出来。而模型名不是机密。"""
    assert "secrets.AIFIX_FIXER__MODEL" not in _RAW
    assert "secrets.AIFIX_DETECTOR__MODEL" not in _RAW


def test_the_thinking_switch_falls_back_to_the_code_default():
    """variable 未设置时 Actions 给的是**空串**，而空串在 config 里被当作
    「不发这个参数」= 随端点默认 = **开**。

    那与「默认关」正好相反 —— 一个纯粹由 YAML 语义造成的、和意图反着来的默认。
    所以这里必须有 `|| 'false'`。
    """
    step = next(s for s in _job()["steps"] if "aifix issue handle" in s.get("run", ""))
    expr = step["env"]["AIFIX_REPRODUCER_THINKING"]
    assert "vars.AIFIX_REPRODUCER_THINKING" in expr
    assert "'false'" in expr, f"缺少兜底，未设置时会变成开：{expr}"
