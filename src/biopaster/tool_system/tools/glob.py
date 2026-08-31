import os
from typing import Any
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext
from pathlib import Path
from fnmatch import fnmatch

class GlobTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Glob",
            description=(
                (("Glob tool to search for files in a directory. Use glob patterns to search for files."
                 "The returned files are sorted by modification time in descending order, "
                 "and will be truncated if the number of files is greater than the max_files limit."
                 ))
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "description": "The path to search for files."},
                    "pattern": {"type": "string", "description": "The glob pattern to search for files."},
                    "max_files": {"type": "integer", 
                                  "default": 10,
                                  "description": "The maximum number of files to return."},
                },
                "required": ["pattern", "path"],
            },
            is_read_only=True,
            max_result_size_chars=20_000,
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = tool_input.get("path", "")
        pattern = tool_input.get("pattern", "")
        max_files = tool_input.get("max_files", 50)
        if not os.path.exists(path):
            return ToolResult(
                name="Glob",
                output=[{
                    "type": "text",
                    "content": f"[error] Path '{path}' does not exist.",
                }],
                is_error=True,
            )
        path = Path(path)
        if not path.is_dir():
            return ToolResult(
                name="Glob",
                output=[{
                    "type": "text",
                    "content": f"[error] Path '{path}' is not a directory.",
                }],
                is_error=True,
            )
        
        if not pattern:
            return ToolResult(
                name="Glob",
                output=[{
                    "type": "text",
                    "content": f"[error] Pattern cannot be empty.",
                }],
                is_error=True,
            )
        
        matched_files = [
            entry
            for entry in path.iterdir()
            if entry.is_file() and fnmatch(entry.name, pattern)
        ]
        matched_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        matched_files = [entry.name for entry in matched_files]
        
        truncated = len(matched_files) > max_files
        matched_files = matched_files[:max_files]
        return ToolResult(
            name="Glob",
            output=[{
                "type": "text",
                "content": {"truncated": truncated,
                            "matched_files": matched_files[:max_files],
                            "file_count": len(matched_files)},
            }],
        )
        
        
if __name__ == "__main__":
    tool = GlobTool()
    print(tool.run({"path": ".", "pattern": "*", "max_files": 50}))
    
