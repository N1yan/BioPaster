from pathlib import Path
from typing import Any
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext

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
        
    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if not file_path:
            return ToolResult(
                name="Write",
                output=[{
                    "type": "text",
                    "content": "[error] File path is not provided.",
                }],
                is_error=True,
            )
        file_path = Path(file_path)
        path = context.ensure_allowed_path(file_path)
        
        if path.exists():
            return ToolResult(
                name="Write",
                output=[{
                    "type": "text",
                    "content": "[error] File already exists.",
                }],
                is_error=True,
            )
       
        path.write_text(content, encoding="utf-8")

        return ToolResult(
            name="Write",
            output= [{
                "type": "text",
                "content": content,
                "file_path": str(path)
            }]
        )
