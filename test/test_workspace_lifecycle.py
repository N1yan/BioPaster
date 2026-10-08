"""Offline coverage of startup paths and persisted workspace transitions."""

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from biopaster import cli
from biopaster.agent.session import load_session
from biopaster.repl import core
from biopaster.tool_system.permissions import PermissionRule


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core, "load_config", lambda: {})
    monkeypatch.setattr(core, "resolve_model_config", lambda config: {"provider": "test"})
    monkeypatch.setattr(core, "get_configured_notebook_kernels", lambda: {})
    monkeypatch.setattr(core, "build_prompt_session", lambda commands: object())
    monkeypatch.setattr(core, "Console", lambda: Console(file=StringIO()))
    monkeypatch.setattr(core.BioPasterStreamingREPL, "run", lambda self: None)
    monkeypatch.setattr(
        core.BioPasterStreamingREPL, "_build_provider",
        lambda self, config: SimpleNamespace(model="test-model"),
    )
    return home


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_workspace_reaches_tools_and_notebook(runtime, tmp_path, monkeypatch, explicit):
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path if explicit else workspace)
    argv = ["biopaster", "--workspace", workspace.name] if explicit else ["biopaster"]
    monkeypatch.setattr("sys.argv", argv)
    contexts = []
    monkeypatch.setattr(core.BioPasterStreamingREPL, "run", lambda self: contexts.append(self.tool_context))

    assert cli.main() == 0
    context = contexts[0]
    assert context.workspace_root == workspace
    assert context.cwd == workspace
    assert context.permission_context.workspace_root == workspace
    assert context.ensure_allowed_path("input.txt") == workspace / "input.txt"
    assert context.notebook_path == workspace / "notebook.ipynb"
    assert not context.notebook_path.exists()


def test_cli_resolves_home_and_symlink(runtime, monkeypatch):
    target = runtime / "actual workspace"
    target.mkdir()
    (runtime / "linked").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr("sys.argv", ["biopaster", "--workspace", "~/linked"])
    contexts = []
    monkeypatch.setattr(core.BioPasterStreamingREPL, "run", lambda self: contexts.append(self.tool_context))
    assert cli.main() == 0
    assert contexts[0].workspace_root == target
    assert contexts[0].notebook_path == target / "notebook.ipynb"


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_workspace_exits_before_creating_session(runtime, tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_text("not a directory")
    monkeypatch.setattr("sys.argv", ["biopaster", "--workspace", str(path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "workspace" in capsys.readouterr().err.lower()
    assert not (runtime / ".biopaster").exists()


@pytest.mark.parametrize("flag", ["--help", "--version", "-v", "-V"])
def test_information_flags_do_not_initialize_repl(runtime, monkeypatch, capsys, flag):
    monkeypatch.setattr("sys.argv", ["biopaster", flag])
    try:
        code = cli.main()
    except SystemExit as error:
        code = error.code
    assert code == 0
    assert capsys.readouterr().out
    assert not (runtime / ".biopaster").exists()


def test_new_session_preserves_workspace_and_resets_permissions(runtime, tmp_path):
    app = core.BioPasterStreamingREPL(workspace_root=tmp_path)
    context = app.tool_context
    context.permission_context.add_rule(PermissionRule("Read"))
    old_id = app.session_log.session_id
    app.conversation.add_user_message("Keep this session")
    app._new_session()
    assert app.session_log.session_id != old_id
    assert app.tool_context.workspace_root == tmp_path
    assert app.tool_context.cwd == context.cwd
    assert app.tool_context.notebook_path == context.notebook_path
    assert not app.tool_context.permission_context.session_rules
    assert load_session(old_id).workspace_root == tmp_path


@pytest.mark.parametrize("missing_workspace", [False, True])
def test_resume_uses_saved_paths_or_keeps_active_session(runtime, tmp_path, missing_workspace):
    workspace_a = tmp_path / "A"
    workspace_b = tmp_path / "B"
    workspace_a.mkdir()
    workspace_b.mkdir()
    saved = core.BioPasterStreamingREPL(workspace_root=workspace_a)
    saved.conversation.add_user_message("Saved in A")
    saved._save_session_snapshot()
    saved_id = saved.session_log.session_id
    snapshot = load_session(saved_id)
    assert snapshot.workspace_root == workspace_a
    assert snapshot.notebook_path == workspace_a / "notebook.ipynb"

    active = core.BioPasterStreamingREPL(workspace_root=workspace_b)
    old_context, old_log = active.tool_context, active.session_log
    if missing_workspace:
        workspace_a.rmdir()
    active._resume_session(saved_id)
    if missing_workspace:
        assert active.tool_context is old_context
        assert active.session_log is old_log
        assert "Unable to resume" in active.console.file.getvalue()
    else:
        assert active.tool_context.workspace_root == workspace_a
        assert active.tool_context.cwd == workspace_a
        assert active.tool_context.notebook_path == snapshot.notebook_path
        assert active.tool_context.permission_context.workspace_root == workspace_a
        assert active.session_log.session_id == saved_id
        assert active.conversation.messages[0].content == "Saved in A"
