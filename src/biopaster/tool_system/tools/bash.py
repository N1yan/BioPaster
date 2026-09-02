import subprocess
from typing import Any

from ..context import ToolContext
from ..protocol import ToolResult
from ..registry import ToolSpec


class BashTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Bash",
            description="Run basic Bash commands. Use executeCodeTool for scientific code.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout_s": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 1,
                    },
                },
                "required": ["command"],
            },
            is_destructive=True,
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        command = tool_input.get("command", "").strip()
        if not command:
            return ToolResult(
                name="Bash",
                output=[{
                    "type": "text",
                    "content": "[error] command cannot be empty",
                }],
                is_error=True,
            )

        cwd = context.ensure_allowed_path(tool_input.get("cwd") or context.cwd)
        if not cwd.is_dir():
            return ToolResult(
                name="Bash",
                output=[{
                    "type": "text",
                    "content": f"[error] cwd is not a directory: {cwd}",
                }],
                is_error=True,
            )

        timeout_s = tool_input.get("timeout_s", 60)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s < 1:
            return ToolResult(
                name="Bash",
                output=[{
                    "type": "text",
                    "content": "[error] timeout_s must be an integer of at least 1",
                }],
                is_error=True,
            )

        context.execute_permission_check(command)
        try:
            completed = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            return ToolResult(
                name="Bash",
                output=[{
                    "type": "text",
                    "content": {
                        "stdout": stdout,
                        "stderr": stderr,
                        "exit_code": None,
                        "cwd": str(cwd),
                        "timed_out": True,
                    },
                }],
                is_error=True,
            )

        return ToolResult(
            name="Bash",
            output=[{
                "type": "text",
                "content": {
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                    "cwd": str(cwd),
                },
            }],
            is_error=completed.returncode != 0,
        )
