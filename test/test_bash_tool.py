from pathlib import Path

from biopaster.tool_system.defaults import build_default_registry
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.permissions import PermissionRule
from biopaster.tool_system.protocol import ToolCall, ToolResult
from biopaster.tool_system.registry import ToolRegistry
from biopaster.tool_system.tools.bash import BashTool


def dispatch_bash(tool_input: dict, context: ToolContext) -> ToolResult:
    context.permission_context.add_rule(PermissionRule("Bash"))
    registry = ToolRegistry([BashTool()])
    result = registry.dispatch(
        ToolCall(name="Bash", input=tool_input, tool_use_id="bash-test"),
        context,
    )
    assert result.tool_use_id == "bash-test"
    return result


def test_bash_executes_command_in_allowed_cwd(tmp_path: Path) -> None:
    result = dispatch_bash(
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
        },
        "metadata": {
            "exit_code": 0,
            "cwd": str(tmp_path),
        },
    }]


def test_bash_rejects_empty_command(tmp_path: Path) -> None:
    result = dispatch_bash(
        {"command": "   "},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": "Tool Bash invalid input: command must be a non-empty string",
        "metadata": {},
    }]


def test_bash_timeout_preserves_partial_output(tmp_path: Path) -> None:
    result = dispatch_bash(
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
        },
        "metadata": {
            "exit_code": None,
            "cwd": str(tmp_path),
            "timed_out": True,
        },
    }]


def test_bash_is_available_in_default_registry() -> None:
    registry = build_default_registry()

    assert "bash" in registry.list_tools()


def test_bash_supports_pipeline_and_redirection(tmp_path: Path) -> None:
    result = dispatch_bash(
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
    result = dispatch_bash(
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
        },
        "metadata": {
            "exit_code": 7,
            "cwd": str(tmp_path),
        },
    }]


def test_bash_rejects_missing_cwd(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = dispatch_bash(
        {"command": "pwd", "cwd": str(missing)},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": f"Tool Bash invalid input: cwd is not a directory: {missing}",
        "metadata": {},
    }]


def test_bash_rejects_invalid_timeout(tmp_path: Path) -> None:
    result = dispatch_bash(
        {"command": "pwd", "timeout_s": 0},
        ToolContext(workspace_root=tmp_path),
    )

    assert result.is_error is True
    assert result.output == [{
        "type": "text",
        "content": "Tool Bash invalid input: timeout_s must be an integer of at least 1",
        "metadata": {},
    }]
