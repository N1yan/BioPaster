from pathlib import Path
from typing import Any
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext
from ..errors import ToolInputError, ToolPermissionError
from ..permissions import (
    PermissionRequest,
    PermissionRule,
    PermissionTarget,
)

class WriteTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Write",
            description="Write a file to the local filesystem.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
            is_destructive=True,
            max_result_size_chars=20_000,
        )
        
    def prepare_input(
          self,
          tool_input: dict[str, Any],
          context: ToolContext,
    ) -> dict[str, Any]:
        unknown_fields = set(tool_input) - {"file_path", "content"}
        if unknown_fields:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown_fields))}"
            )

        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ToolInputError(
                "file_path must be a non-empty string"
            )

        content = tool_input.get("content")
        if not isinstance(content, str):
            raise ToolInputError("content must be a string")

        paths = context.resolve_permission_paths(file_path)
        path = paths[-1]

        if path.exists():
            raise ToolInputError(
                f"File already exists: {path}"
            )

        if not path.parent.is_dir():
            raise ToolInputError(
                f"Parent directory does not exist: {path.parent}"
            )

        return {
            "file_path": str(path),
            "content": content,
            "_permission_paths": paths,
        }
    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        paths = tool_input["_permission_paths"]

        for path in paths:
            if path.name.startswith("BioPaster_evidence_"):
                raise ToolPermissionError(
                    "Evidence files must be created through copyPaste."
                )

        return PermissionRequest(
            tool_name=self.spec().name,
            tool_use_id=None,
            description=(
                f"Create file: {tool_input['file_path']}\n"
                f"Content:\n{tool_input['content']}"
            ),
            targets=tuple(
                PermissionTarget(
                    tool_name="Edit",
                    rule_content=str(path),
                )
                for path in paths
            ),
            suggestions=tuple(
                PermissionRule(
                    tool_name="Edit",
                    rule_content=str(path),
                )
                for path in paths
            ),
        )
    
    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        path = Path(tool_input["file_path"])
        content = tool_input["content"]

        with path.open("x", encoding="utf-8") as file:
            file.write(content)

        return ToolResult(
            name=self.spec().name,
            output=[{
                "type": "text",
                "content": content,
                "file_path": str(path),
            }],
        )
