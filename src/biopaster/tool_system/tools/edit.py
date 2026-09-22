
from ..registry import ToolSpec
from ..protocol import ToolResult
from ..context import ToolContext
from pathlib import Path
from ..errors import ToolInputError
import difflib
from typing import Any
from ..errors import ToolPermissionError
from ..permissions import (
    PermissionRequest,
    PermissionRule,
    PermissionTarget,
)


class EditTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Edit",
            description="Performs exact string replacements in files.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file_path": {"type": "string",
                                  "description": "The path to the target file. And the file name cannot start with: BioPaster_evidence_",
                                },
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {
                        "type": "boolean",
                        "default": False}
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            is_destructive=True,
        )
    
    def prepare_input(
          self,
          tool_input: dict[str, Any],
          context: ToolContext,
    ) -> dict[str, Any]:
        unknown_fields = set(tool_input) - {
            "file_path",
            "old_string",
            "new_string",
            "replace_all",
        }
        if unknown_fields:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown_fields))}"
            )

        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ToolInputError(
                "file_path must be a non-empty string"
            )

        old_string = tool_input.get("old_string")
        if not isinstance(old_string, str) or not old_string:
            raise ToolInputError(
                "old_string must be a non-empty string"
            )

        new_string = tool_input.get("new_string")
        if not isinstance(new_string, str):
            raise ToolInputError(
                "new_string must be a string"
            )

        replace_all = tool_input.get("replace_all", False)
        if not isinstance(replace_all, bool):
            raise ToolInputError(
                "replace_all must be a boolean"
            )

        paths = context.resolve_permission_paths(file_path)
        path = paths[-1]

        if not path.is_file():
            raise ToolInputError(
                f"File does not exist or is not a regular file: {path}"
            )

        return {
            "file_path": str(path),
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
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
                    "Evidence files cannot be edited."
                )

        return PermissionRequest(
            tool_name=self.spec().name,
            tool_use_id=None,
            description=(
                f"Edit file: {tool_input['file_path']}\n"
                f"Replace all: {tool_input['replace_all']}\n\n"
                f"Old text:\n{tool_input['old_string']}\n\n"
                f"New text:\n{tool_input['new_string']}"
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
        old_string = tool_input["old_string"]
        new_string = tool_input["new_string"]
        replace_all = tool_input["replace_all"]

        if not path.is_file():
            raise ToolInputError(
                f"File does not exist or is not a regular file: {path}"
            )

        if not context.was_file_read_and_unchanged(path):
            raise ToolInputError("File cannot be edited: it must be read first and remain unchanged since the last read.")
            
        original_content = path.read_text(encoding="utf-8")
        count = original_content.count(old_string)
        if count == 0:
            raise ToolInputError("old_string not found in file")
        if count > 1 and not replace_all:
            raise ToolInputError("old_string is not unique; provide a larger old_string or set replace_all=true")
        
        if replace_all:
            updated = original_content.replace(old_string, new_string)
        else:
            updated = original_content.replace(old_string, new_string, 1)
            
        path.write_text(updated, encoding="utf-8")
        context.mark_file_read(path)
        
        origin_lines = original_content.splitlines(keepends=True)
        updated_lines = updated.splitlines(keepends=True)
        
        diff_lines = list(
            difflib.unified_diff(
                origin_lines,
                updated_lines,
                fromfile=str(path),
                tofile=str(path),
                n=3,
                lineterm="",
            )
        )

        return ToolResult(
            name="Edit",
            output=[{
                "type":"text",
                "content":{
                "file_path": str(path),
                "old_string": old_string,
                "new_string": new_string,
                "diff_lines": diff_lines,
                "replace_all": bool(replace_all),}
            }])
        