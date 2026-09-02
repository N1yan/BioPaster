import re
from ..registry import ToolSpec
from ..protocol import ToolResult
from ..context import ToolContext
from typing import Any
from pathlib import Path
import tempfile
import os

import nbformat
from nbclient import NotebookClient
from jupyter_client import AsyncKernelManager

def _run_notebook_cell(
    code: str,
    timeout: int = 60,
    kernel_name: str = "python3",
    cwd: str | None = None
):
    cell = nbformat.v4.new_code_cell(code)
    notebook =  nbformat.v4.new_notebook(cells=[cell])
    kernel_manager = AsyncKernelManager(
        kernel_name=kernel_name,
        transport_encryption="required",
    )
    client = NotebookClient(
        notebook,
        timeout=timeout,
        km=kernel_manager,
        allow_errors=True,
        resources={"metadata": {"path": cwd or "."}})
    client.execute()
    
    return notebook

def _notebook_output_parse(notebook: nbformat.NotebookNode) -> tuple[list[dict], bool]:
    tool_outputs = []
    has_error = False
    for output in notebook.cells[0].outputs:
        if output.output_type == "stream":
            tool_outputs.append({
                "type": "text",
                "content": output.text,
            })
        elif output.output_type in {"execute_result", "display_data"}:
            if "text/plain" in output.data:
                tool_outputs.append({
                    "type": "text",
                    "content": output.data["text/plain"],
                })
            if "image/png" in output.data:
                tool_outputs.append({
                    "type": "image",
                    "media_type": "image/png",
                    "content": output.data["image/png"],
                })
        elif output.output_type == "error":
            has_error = True
            tool_outputs.append({
                "type": "text",
                "content": "\n".join(output.traceback),
            })
    return tool_outputs, has_error

def _save_cell(
    executed_notebook: nbformat.NotebookNode,
    notebook_path: Path,
    kernel_name: str,
) -> None:
    notebook_path.parent.mkdir(parents=True, exist_ok=True)

    if notebook_path.exists():
        saved_notebook = nbformat.read(notebook_path, as_version=4)
    else:
        saved_notebook = nbformat.v4.new_notebook(
            metadata={
                "kernelspec": {
                    "name": kernel_name,
                    "display_name": kernel_name,
                }
            }
        )
    saved_notebook.cells.append(executed_notebook.cells[0])

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".ipynb",
            prefix=".biopaster-",
            dir=notebook_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            nbformat.write(saved_notebook, temporary_file)

        os.replace(temporary_path, notebook_path)

    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

CONTENT_HEADER = f"#{'-'*20}Copied Content{'-'*20}"
ANNOTATION_HEADER = f"#{'-'*20}Annotation{'-'*20}"

BLOCK_PATTERN = re.compile(
    rf"^{re.escape(ANNOTATION_HEADER)}[ \t]*\r?\n"
    rf"(?P<annotation>.*?)"
    rf"^{re.escape(CONTENT_HEADER)}[ \t]*\r?\n"
    rf"(?P<code>.*?)"
    rf"(?=^{re.escape(ANNOTATION_HEADER)}[ \t]*\r?$|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)
def _script_parse(script_path: Path) -> str:
    content = script_path.read_text(encoding="utf-8")
    blocks: list[tuple[str, str]] = []

    for match in BLOCK_PATTERN.finditer(content):
        annotation = match.group("annotation").strip()
        code = match.group("code").strip()
        blocks.append((annotation, code))

    if not blocks:
        return content

    merged: list[tuple[str, str]] = []

    for annotation, code in blocks:
        if merged and merged[-1][0] == annotation:
            previous_annotation, previous_code = merged[-1]
            merged[-1] = (
                previous_annotation,
                previous_code + "\n\n" + code,
            )
        else:
            merged.append((annotation, code))

    return "\n\n".join(
        f"{annotation}\n{code}" if annotation else code
        for annotation, code in merged
    )

DEFAULT_KERNELS = {
    "python": "python3",
    "python3": "python3",
    "r": "ir",
    "bash": "bash",
}

class ExecuteCodeTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="executeCode",
            description=(
                "Execute code in a Jupyter notebook cell."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "script_path": {"type": "string",
                                    "description": "Path to the script file to execute, but can only use for files whose name starts with: BioPaster_evidence_."
                                    },
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 60},
                    "language": {"type": "string"},
                },
                "oneOf": [
                    {"required": ["command", "language"],
                     "not": {"required": ["script_path"]}},
                    {"required": ["script_path", "language"],
                     "not": {"required": ["command"]}},
                ]
            },
            is_destructive=True,
            max_result_size_chars=20_000,
        )
        
    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        
        command = tool_input.get("command", "").strip()
        script_path = tool_input.get("script_path", "").strip()
        cwd = tool_input.get("cwd", None)
        timeout_s = tool_input.get("timeout_s", 60)
        language = tool_input.get("language", "")
        notebook_path = context.notebook_path
        
        if bool(command) == bool(script_path):
            return ToolResult(
                name=self.name,
                output="Exactly one of 'command' or 'script_path' must be provided.",
                is_error=True,
            )
        if script_path:
            script_path = context.ensure_allowed_path(script_path)
            if not script_path.exists():
                return ToolResult(
                    name="executeCode",
                    output=[{
                        "type": "text",
                        "content": f"[error] script_path does not exist: {script_path}",
                    }],
                    is_error=True,
                )
            command = _script_parse(script_path)
            
        if not language:
            return ToolResult(
                name="executeCode",
                output=[{
                    "type": "text",
                    "content": "[error] language is not specified",
                }],
                is_error=True,
            )
        
        if context.kernels.get(language.lower()):
            kernel_name = context.kernels[language.lower()]
        else:
            kernel_name = DEFAULT_KERNELS.get(language.lower())
            if not kernel_name:
                return ToolResult(
                    name="executeCode",
                    output=[{
                        "type": "text",
                        "content": f"[error] unsupported language: {language}",
                    }],
                    is_error=True,
                )
            
        if context.notebook_path is None:
            return ToolResult(
                name="executeCode",
                output=[{
                    "type": "text",
                    "content": "[error] notebook_path is not configured",
                }],
                is_error=True,
            )

        notebook_path = context.ensure_allowed_path(context.notebook_path)
        
        cwd = cwd or context.cwd
        if cwd is not None:
            cwd = context.ensure_allowed_path(cwd)
        
        context.execute_permission_check(command)
        notebook = _run_notebook_cell(
            code=command,
            timeout=timeout_s,
            kernel_name=kernel_name,
            cwd=cwd
        )

        tool_outputs, has_error = _notebook_output_parse(notebook)

        if has_error:
            return ToolResult(
                name="executeCode",
                output=tool_outputs,
                is_error=True,
            )

        try:
            _save_cell(
                executed_notebook=notebook,
                notebook_path=notebook_path,
                kernel_name=kernel_name,
            )
            tool_outputs.append({
            "type": "text",
            "content": f"Executed successfully, and it was saved to: {notebook_path}",
            })
            print("\033[33m[ExecuteCodeTool]\033[0m")
            return ToolResult(
                name="executeCode",
                output=tool_outputs,
                is_error=False,
            )
        except Exception as e:
            tool_outputs.append({
            "type": "text",
            "content": f"[error] failed to save notebook: {e}",
            })
            return ToolResult(
                name="executeCode",
                output=tool_outputs,
                is_error=True,
            )

        
