import subprocess
from typing import Any

from ..context import ToolContext
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..permissions import (
    PermissionRequest,
    PermissionRule,
    PermissionTarget,
)
from ..errors import ToolInputError


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
        
    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        unknown_fields = set(tool_input) - {
            "command", "cwd", "timeout_s"
        }
        if unknown_fields:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown_fields))}"
            )

        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolInputError(
                "command must be a non-empty string"
            )

        cwd = tool_input.get("cwd")
        if cwd is None:
            cwd = str(context.cwd)
        elif not isinstance(cwd, str) or not cwd.strip():
            raise ToolInputError(
                "cwd must be a non-empty string"
            )

        resolved_cwd = context.resolve_permission_paths(cwd)[-1]
        if not resolved_cwd.is_dir():
            raise ToolInputError(
                f"cwd is not a directory: {resolved_cwd}"
            )

        timeout_s = tool_input.get("timeout_s", 60)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int)
            or timeout_s < 1
        ):
            raise ToolInputError(
                "timeout_s must be an integer of at least 1"
            )

        return {
            "command": command,
            "cwd": cwd,
            "timeout_s": timeout_s,
        }
            
    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        command = tool_input["command"]
        context.permission_context.check_command_restrictions(command)
        tool_name = self.spec().name

        cwd = tool_input.get("cwd") or context.cwd
        cwd_paths = context.resolve_permission_paths(cwd)

        return PermissionRequest(
            tool_name=tool_name,
            tool_use_id=None,
            description=(
                f"Working directory: {cwd_paths[-1]}\n"
                f"Command:\n{command}"
            ),
            targets=(
                PermissionTarget(
                    tool_name=tool_name,
                    rule_content=command,
                ),
                *(
                    PermissionTarget(
                        tool_name="Read",
                        rule_content=str(path),
                    )
                    for path in cwd_paths
                ),
            ),
            suggestions=(
                PermissionRule(
                    tool_name=tool_name,
                    rule_content=command,
                ),
                PermissionRule(
                    tool_name=tool_name,
                ),
            ),
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        command = tool_input["command"]
        cwd = context.resolve_permission_paths(tool_input["cwd"])[-1]
        timeout_s = tool_input["timeout_s"]
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
