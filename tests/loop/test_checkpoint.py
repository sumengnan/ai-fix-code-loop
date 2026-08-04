from aifix.config import AifixConfig
from aifix.graph import build_graph, checkpointer_for


def test_checkpointer_disabled_by_default(tmp_path):
    assert checkpointer_for(AifixConfig(), tmp_path) is None


def test_checkpointer_created_when_enabled(tmp_path):
    cp = checkpointer_for(AifixConfig(enable_checkpoint=True), tmp_path)
    assert cp is not None
    assert (tmp_path / "checkpoint.sqlite").exists()


def test_checkpointer_creates_missing_directory(tmp_path):
    d = tmp_path / "deep" / "nested"
    assert checkpointer_for(AifixConfig(enable_checkpoint=True), d) is not None
    assert (d / "checkpoint.sqlite").is_file()


def test_graph_compiles_with_checkpointer(tmp_path):
    cp = checkpointer_for(AifixConfig(enable_checkpoint=True), tmp_path)
    g = build_graph(checkpointer=cp)
    names = {n for n in g.get_graph().nodes if not n.startswith("__")}
    assert names == {"preflight", "baseline", "take_next",
                     "detect", "fix", "verify", "report"}


def test_graph_compiles_without_checkpointer():
    g = build_graph()
    assert g is not None
