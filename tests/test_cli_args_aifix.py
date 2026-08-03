def test_cmd_mutate_refuses_a_repo_no_adapter_claims(tmp_path, capsys):
    """认领不了要当场退出，不能走到 mkdir 甩 OSError 调用栈。

    _cmd_mine 对同一个路径给的是人话「没有适配器认领这个项目」，
    _cmd_mutate 也应该一样，而不是在 mutate_tasks 的 workdir.mkdir()
    那里炸成一个 Read-only file system 的 OSError。
    """
    import aifix.cli as cli_mod

    args = cli_mod.build_parser().parse_args(
        ["mutate", str(tmp_path), "--out", str(tmp_path / "tasks.jsonl")])
    with pytest.raises(SystemExit) as exc:
        cli_mod._cmd_mutate(args)

    assert exc.value.code == 1
    msg = capsys.readouterr().out
    assert "适配器" in msg, "要用人话告诉用户没人认领这个路径"
    assert "Traceback" not in msg
    assert "OSError" not in msg