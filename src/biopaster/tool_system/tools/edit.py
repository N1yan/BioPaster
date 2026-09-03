
from ..registry import ToolSpec
from ..protocol import ToolResult
from ..context import ToolContext
from pathlib import Path
from ..errors import ToolInputError
import difflib
from typing import Any


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
        
    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = tool_input.get("file_path", "")
        old_string = tool_input["old_string"]
        new_string = tool_input["new_string"]
        replace_all = tool_input.get("replace_all", False)

        if not file_path:
            raise  ToolInputError("File path is not provided.")
        
        file_path = Path(file_path)
        
        if file_path.name.startswith("BioPaster_evidence_"):
            raise ToolInputError("File name cannot start with: BioPaster_evidence_")
            
        path = context.ensure_allowed_path(file_path)
        
        if not path.exists():
            raise ToolInputError("File does not exist.")
            
        if not context.was_file_read_and_unchanged(path):
            raise ToolInputError("File cannot be edited: file must be read first and unchanged since last read.")
            
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
        