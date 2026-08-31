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
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 60},
                    "language": {"type": "string"},
                },
                "required": ["command", "language"],
            },
            is_destructive=True,
            max_result_size_chars=20_000,
        )
        
    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        
        command = tool_input.get("command", "").strip()
        cwd = tool_input.get("cwd", None)
        timeout_s = tool_input.get("timeout_s", 60)
        language = tool_input.get("language", "")
        notebook_path = context.notebook_path
        
        if not command:
            return ToolResult(
                name="executeCode",
                output=[{
                    "type": "text",
                    "content": "[error] command is empty",
                }],
                is_error=True,
            )
            
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

        
