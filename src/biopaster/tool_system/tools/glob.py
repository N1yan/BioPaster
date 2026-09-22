from typing import Any
from ..protocol import ToolResult
from ..registry import ToolSpec
from ..context import ToolContext
from pathlib import Path
from fnmatch import fnmatch
from ..errors import ToolInputError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget

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

    def prepare_input(self, tool_input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        unknown = set(tool_input) - {"path", "pattern", "max_files"}
        if unknown:
            raise ToolInputError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        path = tool_input.get("path")
        pattern = tool_input.get("pattern")
        max_files = tool_input.get("max_files", 50)
        if not isinstance(path, str) or not path.strip() or not isinstance(pattern, str) or not pattern:
            raise ToolInputError("path and pattern cannot be empty")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise ToolInputError("max_files must be a positive integer")
        paths = context.resolve_permission_paths(path)
        if not paths[-1].is_dir():
            raise ToolInputError(f"Path is not a directory: {paths[-1]}")
        return {"path": str(paths[-1]), "pattern": pattern,
                "max_files": max_files, "_permission_paths": paths}

    def check_permissions(self, tool_input, context):
        targets = tuple(PermissionTarget("Read", str(p)) for p in tool_input["_permission_paths"])
        return PermissionRequest(
            self.spec().name, None, f"List files in: {tool_input['path']}",
            targets, tuple(PermissionRule("Read", t.rule_content) for t in targets),
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(tool_input["path"])
        matched_files = []
        for entry in path.iterdir():
            if not fnmatch(entry.name, tool_input["pattern"]):
                continue
            targets = [
                PermissionTarget("Read", str(candidate))
                for root in tool_input["_permission_paths"]
                for candidate in context.resolve_permission_paths(root / entry.name)
            ]
            decisions = [context.permission_context.evaluate(t) for t in targets]
            if any(d.behavior == "deny" or
                   (d.behavior == "ask" and d.matched_rule is not None)
                   for d in decisions):
                continue
            # A directory listing approval does not approve symlink targets elsewhere.
            if entry.is_symlink() and any(d.behavior != "allow" for d in decisions):
                continue
            if entry.is_file():
                matched_files.append(entry)
        matched_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        max_files = tool_input["max_files"]
        truncated = len(matched_files) > max_files
        names = [entry.name for entry in matched_files[:max_files]]
        return ToolResult(
            name="Glob",
            output=[{"type": "text", "content": {
                "truncated": truncated, "matched_files": names, "file_count": len(names),
            }}],
        )
        
        
if __name__ == "__main__":
    tool = GlobTool()
    print(tool.run({"path": ".", "pattern": "*", "max_files": 50}))
    
