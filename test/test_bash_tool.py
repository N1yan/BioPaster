from pathlib import Path

from biopaster.tool_system.defaults import build_default_registry
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.tools.bash import BashTool


def test_bash_executes_command_in_allowed_cwd(tmp_path: Path) -> None:
    result = BashTool().run(
        {
            "command": "printf 'hello'",
            "cwd": str(tmp_path),
        },
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is False
    assert result.output == [{
        "type": "text",
        "content": {
            "stdout": "hello",
            "stderr": "",
            "exit_code": 0,
            "cwd": str(tmp_path),
        },
    }]


def test_bash_rejects_empty_command(tmp_path: Path) -> None:
    result = BashTool().run(
        {"command": "   "},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": "[error] command cannot be empty",
    }]


def test_bash_timeout_preserves_partial_output(tmp_path: Path) -> None:
    result = BashTool().run(
        {
            "command": "printf before; sleep 2",
            "cwd": str(tmp_path),
            "timeout_s": 1,
        },
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": {
            "stdout": "before",
            "stderr": "",
            "exit_code": None,
            "cwd": str(tmp_path),
            "timed_out": True,
        },
    }]


def test_bash_is_available_in_default_registry() -> None:
    registry = build_default_registry()

    assert "bash" in registry.list_tools()


def test_bash_supports_pipeline_and_redirection(tmp_path: Path) -> None:
    result = BashTool().run(
        {
            "command": "printf 'beta\\nalpha\\n' | sort > sorted.txt && cat sorted.txt",
            "cwd": str(tmp_path),
        },
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is False
    assert result.output[0]["content"]["stdout"] == "alpha\nbeta\n"
    assert (tmp_path / "sorted.txt").read_text() == "alpha\nbeta\n"


def test_bash_nonzero_exit_preserves_stdout_and_stderr(tmp_path: Path) -> None:
    result = BashTool().run(
        {
            "command": "printf output; printf error >&2; exit 7",
            "cwd": str(tmp_path),
        },
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": {
            "stdout": "output",
            "stderr": "error",
            "exit_code": 7,
            "cwd": str(tmp_path),
        },
    }]


def test_bash_rejects_missing_cwd(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = BashTool().run(
        {"command": "pwd", "cwd": str(missing)},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": f"[error] cwd is not a directory: {missing}",
    }]


def test_bash_rejects_invalid_timeout(tmp_path: Path) -> None:
    result = BashTool().run(
        {"command": "pwd", "timeout_s": 0},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": "[error] timeout_s must be an integer of at least 1",
    }]
