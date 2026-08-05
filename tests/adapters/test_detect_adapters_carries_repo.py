"""`detect_adapters` 造出来的实例必须知道自己的 `pkg_dir`。

VitestAdapter 靠 `repo` 探 `package.json` 在哪个子目录。不给的话 `pkg_dir` 是
空串，于是前后端同仓的工程里：

- `test_dirs()` 报 `src` 而不是 `web/src` —— reproducer 会把前端测试写进后端的
  源码目录，而那个路径**照样**被 `is_test_path` 认领（它按后缀判），于是校验放行、
  vitest 跑不到它
- `_bin()` 指向根目录那个不存在的 `node_modules/.bin/vitest`

这个缺陷长期休眠：`detect_adapters` 的产物此前只用来回答「有没有适配器认领」，
真正跑测试的实例走 `adapters_from_state` → `adapter_for(repo=...)`。reproducer
开始自己选测试体系之后（0.6.0），选中的实例会一路用到提示词和红检。
"""
import json

from aifix.nodes.baseline import detect_adapters


def _frontend_repo(root):
    """前后端同仓：前端在 web/ 下，后端在根。"""
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "w", "devDependencies": {"vitest": "^2.0.0"}}),
        encoding="utf-8")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


def test_vitest_knows_its_package_subdir(tmp_path):
    ads = {a.name: a for a in detect_adapters(_frontend_repo(tmp_path))}
    assert "vitest" in ads, "前端在子目录时也该被认领"
    assert ads["vitest"].test_dirs() == ["web/src"]


def test_the_test_dirs_are_repo_relative(tmp_path):
    """报出来的目录必须是**仓库相对**的 —— 它直接进 reproducer 的提示词。

    给 `src` 的后果不是「路径不好看」：根目录下往往真有一个 `src/`（本仓库
    就有），模型会把前端测试写进去，而那里没有 vitest 会跑的东西。
    """
    ads = {a.name: a for a in detect_adapters(_frontend_repo(tmp_path))}
    for d in ads["vitest"].test_dirs():
        assert (tmp_path / d).is_dir(), f"{d} 在仓库里不存在"


def test_a_root_level_frontend_still_works(tmp_path):
    """前端就在仓库根时 `pkg_dir` 是空串，目录仍该是 `src`（不是 `/src`）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "w", "devDependencies": {"vitest": "^2.0.0"}}),
        encoding="utf-8")
    ads = {a.name: a for a in detect_adapters(tmp_path)}
    assert ads["vitest"].test_dirs() == ["src"]
