"""Configuration failures must be actionable and preserve user files."""

import json
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from biopaster import cli, config
from biopaster.repl import core


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".biopaster" / "config.json"
    path.parent.mkdir()
    monkeypatch.setattr("sys.argv", ["biopaster", "--workspace", str(tmp_path)])
    return path


def valid_config():
    data = config.get_default_config()
    data["providers"]["openrouter"]["api_key"] = "test-secret-do-not-print"
    return data


def test_first_launch_creates_template_and_exits(settings, capsys):
    assert cli.main() == 1
    output = capsys.readouterr().err
    assert str(settings) in output
    assert "Created" in output
    assert "api_key" in output
    assert "biopaster" in output
    assert json.loads(settings.read_text())["providers"]["openrouter"]["api_key"] == ""
    assert not (settings.parent / "sessions").exists()


@pytest.mark.parametrize("case, expected", [
    ("json", "line 2"),
    ("root", "object"),
    ("empty_key", "openrouter.api_key"),
    ("unknown_model", "default_model"),
    ("invalid_budget", "token budgets"),
])
def test_invalid_config_reports_error_without_overwriting(settings, capsys, case, expected):
    data = valid_config()
    if case == "empty_key":
        data["providers"]["openrouter"]["api_key"] = ""
    elif case == "unknown_model":
        data["default_model"] = "missing"
    elif case == "invalid_budget":
        data["providers"]["openrouter"]["models"][0]["context_window"] = 1
    content = '{\n invalid JSON' if case == "json" else '[]' if case == "root" else json.dumps(data)
    settings.write_text(content)
    assert cli.main() == 1
    output = capsys.readouterr().err
    assert expected in output
    assert str(settings) in output
    assert "test-secret-do-not-print" not in output
    assert settings.read_text() == content
    assert not (settings.parent / "sessions").exists()


def test_invalid_json_is_not_replaced_by_defaults(settings):
    settings.write_text("{")
    with pytest.raises(ValueError, match="line.*column"):
        config.load_config()
    assert settings.read_text() == "{"


def test_valid_startup_and_failed_model_reload_preserve_state(settings, monkeypatch):
    original = json.dumps(valid_config())
    settings.write_text(original)
    monkeypatch.setattr(core, "build_prompt_session", lambda commands: object())
    monkeypatch.setattr(core, "get_configured_notebook_kernels", lambda: {})
    monkeypatch.setattr(core, "Console", lambda: Console(file=StringIO()))
    monkeypatch.setattr(core.BioPasterStreamingREPL, "_build_provider",
                        lambda self, option: SimpleNamespace(model=option["model"]))
    apps = []
    monkeypatch.setattr(core.BioPasterStreamingREPL, "run", lambda self: apps.append(self))
    assert cli.main() == 0
    assert settings.read_text() == original
    app = apps[0]
    provider, context = app.provider, app.tool_context
    settings.write_text("{")
    app._select_model()
    assert app.provider is provider
    assert app.tool_context is context
    assert "line" in app.console.file.getvalue()
    assert settings.read_text() == "{"
