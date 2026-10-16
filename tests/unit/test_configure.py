import json

import pytest

from iris import configure as cfg

# Python 3.11+ (stdlib TOML reader); Codex config checks need it.
tomllib = pytest.importorskip("tomllib")


@pytest.fixture
def spec():
    return cfg.LaunchSpec(
        command=r"C:\Python312\python.exe",
        args=[r"C:\tools\iris\server.py"],
        env={"IRIS_TELEMETRY": "0"},
    )


def test_settings_env_only_non_defaults():
    assert cfg.Settings().env() == {}
    s = cfg.Settings(log_dir=" D:/logs ", telemetry=False, ocr_psm=6, uia_walk=True)
    assert s.env() == {
        "IRIS_LOG_DIR": "D:/logs",
        "IRIS_TELEMETRY": "0",
        "IRIS_OCR_PSM": "6",
        "IRIS_UIA_WALK": "1",
    }


def test_settings_from_dict_ignores_unknown_keys():
    s = cfg.Settings.from_dict({"python": "x", "bogus": 1})
    assert s.python == "x"


def test_console_python_swaps_pythonw(tmp_path):
    (tmp_path / "python.exe").write_text("")
    assert cfg.console_python(tmp_path / "pythonw.exe") == str(tmp_path / "python.exe")
    assert cfg.console_python(tmp_path / "python.exe") == str(tmp_path / "python.exe")


def test_launch_spec_uses_server_py_in_checkout():
    spec = cfg.launch_spec(cfg.Settings(python="py.exe"))
    assert spec.command == "py.exe"
    assert spec.args == [str(cfg.REPO_ROOT / "server.py")]


def test_toml_insert_into_empty(spec):
    out = cfg.upsert_toml_server("", spec)
    data = tomllib.loads(out)["mcp_servers"]["iris"]
    assert data["command"] == spec.command
    assert data["args"] == spec.args
    assert data["env"] == {"IRIS_TELEMETRY": "0"}


def test_toml_replace_keeps_other_tables(spec):
    original = (
        'model = "gpt-5"\n\n'
        "[mcp_servers.iris]\n"
        'command = "old"\n\n'
        "[mcp_servers.iris.env]\n"
        'X = "1"\n\n'
        "[mcp_servers.other]\n"
        'command = "keep"\n\n'
        '[projects."C:\\\\work"]\n'
        'trust_level = "trusted"\n'
    )
    out = cfg.upsert_toml_server(original, spec)
    data = tomllib.loads(out)
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["other"]["command"] == "keep"
    assert data["mcp_servers"]["iris"]["command"] == spec.command
    assert "X" not in data["mcp_servers"]["iris"].get("env", {})
    assert data["projects"]["C:\\work"]["trust_level"] == "trusted"
    assert out.count("[mcp_servers.iris]") == 1


def test_toml_remove_handles_quoted_header():
    text = '[mcp_servers."iris"]\ncommand = "x"\n\n[mcp_servers.iris-old]\ncommand = "y"\n'
    out = cfg.remove_toml_server(text)
    assert "iris-old" in out and '"x"' not in out


def test_toml_escapes_paths_and_quotes():
    spec = cfg.LaunchSpec(command='C:\\a "b"\\python.exe', args=[], env={})
    data = tomllib.loads(cfg.upsert_toml_server("", spec))
    assert data["mcp_servers"]["iris"]["command"] == 'C:\\a "b"\\python.exe'


def test_guide_install_is_idempotent_and_removable():
    base = "# My rules\n\nBe nice.\n"
    once = cfg.install_guide_text(base, "GUIDE v1")
    twice = cfg.install_guide_text(once, "GUIDE v2")
    assert twice.count(cfg.GUIDE_BEGIN) == 1
    assert "GUIDE v2" in twice and "GUIDE v1" not in twice
    assert cfg.remove_guide_text(twice).strip() == base.strip()


def test_claude_desktop_register_merges(tmp_path, monkeypatch, spec):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    conf = tmp_path / "Claude" / "claude_desktop_config.json"
    conf.parent.mkdir()
    conf.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    client = cfg.ClaudeDesktop()
    assert client.status(spec).label == "not connected"
    client.register(spec)
    data = json.loads(conf.read_text())
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["iris"]["command"] == spec.command
    assert client.status(spec).label == "connected"
    assert list(tmp_path.glob("Claude/*.iris-backup-*"))
    client.unregister()
    assert "iris" not in json.loads(conf.read_text())["mcpServers"]


def test_claude_desktop_refuses_malformed_json(tmp_path, monkeypatch, spec):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    conf = tmp_path / "Claude" / "claude_desktop_config.json"
    conf.parent.mkdir()
    conf.write_text("{not json")
    with pytest.raises(ValueError):
        cfg.ClaudeDesktop().register(spec)
    assert conf.read_text() == "{not json"


def test_codex_register_and_status(tmp_path, monkeypatch, spec):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "x"\n')
    client = cfg.Codex()
    assert client.status(spec).label == "not connected"
    client.register(spec)
    assert client.status(spec).label == "connected"
    changed = cfg.LaunchSpec(spec.command, spec.args, {})
    assert client.status(changed).label == "connected (different settings)"
    client.unregister()
    assert tomllib.loads((tmp_path / "config.toml").read_text()) == {"model": "x"}


def test_claude_code_add_argv_puts_env_after_name(spec):
    argv = cfg.ClaudeCode()._add_argv(spec)
    assert argv[:6] == ["claude", "mcp", "add", "--scope", "user", "iris"]
    assert argv[6:8] == ["-e", "IRIS_TELEMETRY=0"]
    assert argv[8:] == ["--", spec.command, *spec.args]


def test_toml_remove_keeps_comment_above_next_table():
    text = (
        "[mcp_servers.iris]\n"
        'command = "x"\n'
        "\n"
        "# my playwright server, keep this\n"
        "[mcp_servers.playwright]\n"
        'command = "npx"\n'
    )
    out = cfg.remove_toml_server(text)
    assert "# my playwright server, keep this\n[mcp_servers.playwright]" in out
    assert '"x"' not in out


def test_toml_remove_drops_trailing_comments_at_eof():
    out = cfg.remove_toml_server('model = "m"\n\n[mcp_servers.iris]\ncommand = "x"\n# tail\n')
    assert out == 'model = "m"\n\n'


@pytest.mark.parametrize("content", ["", "  \n", '\ufeff{"theme": "dark"}', '{"mcpServers": null}'])
def test_claude_desktop_tolerates_empty_bom_and_null(tmp_path, monkeypatch, spec, content):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    conf = tmp_path / "Claude" / "claude_desktop_config.json"
    conf.parent.mkdir()
    conf.write_text(content, encoding="utf-8")
    client = cfg.ClaudeDesktop()
    assert client.current_entry() is None
    client.register(spec)
    assert json.loads(conf.read_text(encoding="utf-8"))["mcpServers"]["iris"]["command"] == spec.command


def test_claude_desktop_prefers_store_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    roaming = tmp_path / "roaming" / "Claude" / "claude_desktop_config.json"
    store = tmp_path / "local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    for p in (roaming, store):
        p.parent.mkdir(parents=True)
        p.write_text("{}")
    assert cfg.ClaudeDesktop().config_path() == store


def test_backups_never_overwrite_each_other(tmp_path):
    f = tmp_path / "c.json"
    f.write_text("v1")
    first = cfg.backup(f)
    f.write_text("v2")
    second = cfg.backup(f)
    assert first != second
    assert first.read_text() == "v1" and second.read_text() == "v2"


def test_cmd_shim_rejects_metacharacters():
    with pytest.raises(ValueError):
        cfg._check_shim_args(r"C:\npm\claude.cmd", ["claude", "-e", r"IRIS_LOG_DIR=C:\R&D"])
    cfg._check_shim_args(r"C:\npm\claude.cmd", ["claude", r"C:\Program Files\py.exe"])
    cfg._check_shim_args(r"C:\bin\claude.exe", ["claude", r"C:\R&D"])


def test_claude_code_restores_old_entry_when_add_fails(monkeypatch, spec):
    client = cfg.ClaudeCode()
    old = {"type": "stdio", "command": "old.exe", "args": ["old.py"]}
    calls = []

    def fake_run(argv, timeout=120):
        calls.append(argv)
        if argv[2] == "add" and argv[-1] == spec.args[-1]:
            return 1, "boom"
        return 0, ""

    monkeypatch.setattr(client, "_cli", lambda: "claude.exe")
    monkeypatch.setattr(client, "current_entry", lambda: old)
    monkeypatch.setattr(cfg, "_run", fake_run)
    with pytest.raises(RuntimeError, match="Previous entry restored"):
        client.register(spec)
    assert calls[-1][-2:] == ["old.exe", "old.py"]
