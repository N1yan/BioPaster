# import re  # Required only by the temporarily disabled script_path support.
from ..registry import ToolSpec
from ..protocol import ToolResult
from ..context import ToolContext
from ..errors import ToolInputError, ToolPermissionError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget
from typing import Any, Mapping
from pathlib import Path
import tempfile
import os

import nbformat
from nbclient import NotebookClient
from jupyter_client import AsyncKernelManager

REFERENCE_HEADER = "#--------------------Reference--------------------"
COPIED_CONTENT_HEADER = "#--------------------Copied Content--------------------"


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
    client.execute(cleanup_kc=True)
    
    return notebook

def _notebook_output_parse(notebook: nbformat.NotebookNode) -> tuple[list[dict], bool]:
    tool_outputs = []
    has_error = False
    for output in notebook.cells[0].outputs:
        if output.output_type == "stream":
            tool_outputs.append({
                "type": "text",
                "content": output.text,
                "metadata": {"output_type": "stream", "name": output.get("name", "stdout")},
            })
        elif output.output_type in {"execute_result", "display_data"}:
            if "text/plain" in output.data:
                tool_outputs.append({
                    "type": "text",
                    "content": output.data["text/plain"],
                    "metadata": {"output_type": output.output_type},
                })
            if "image/png" in output.data:
                tool_outputs.append({
                    "type": "image",
                    "content": output.data["image/png"],
                    "metadata": {"media_type": "image/png", "output_type": output.output_type},
                })
        elif output.output_type == "error":
            has_error = True
            tool_outputs.append({
                "type": "text",
                "content": "\n".join(output.traceback),
                "metadata": {"output_type": "error", "ename": output.ename, "evalue": output.evalue},
            })
    return tool_outputs, has_error

def _save_cells(
    cells: list[nbformat.NotebookNode],
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

    saved_notebook.cells.extend(cells)

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

# script_path support is temporarily disabled.
# CONTENT_HEADER = f"#{'-'*20}Copied Content{'-'*20}"
# ANNOTATION_HEADER = f"#{'-'*20}Annotation{'-'*20}"
#
# BLOCK_PATTERN = re.compile(
#     rf"^{re.escape(ANNOTATION_HEADER)}[ \t]*\r?\n"
#     rf"(?P<annotation>.*?)"
#     rf"^{re.escape(CONTENT_HEADER)}[ \t]*\r?\n"
#     rf"(?P<code>.*?)"
#     rf"(?=^{re.escape(ANNOTATION_HEADER)}[ \t]*\r?$|\Z)",
#     flags=re.MULTILINE | re.DOTALL,
# )
# def _script_parse(script_path: Path) -> str:
#     content = script_path.read_text(encoding="utf-8")
#     blocks: list[tuple[str, str]] = []
#
#     for match in BLOCK_PATTERN.finditer(content):
#         annotation = match.group("annotation").strip()
#         code = match.group("code").strip()
#         blocks.append((annotation, code))
#
#     if not blocks:
#         return content
#
#     merged: list[tuple[str, str]] = []
#
#     for annotation, code in blocks:
#         if merged and merged[-1][0] == annotation:
#             previous_annotation, previous_code = merged[-1]
#             merged[-1] = (
#                 previous_annotation,
#                 previous_code + "\n\n" + code,
#             )
#         else:
#             merged.append((annotation, code))
#
#     return "\n\n".join(
#         f"{annotation}\n{code}" if annotation else code
#         for annotation, code in merged
#     )

class ExecuteCodeTool:
    def __init__(
        self,
        kernels: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        configured = kernels or {
            "python3": {
                "display_name": "Python 3",
                "language": "python",
                "environment": "current",
            }
        }
        self.kernels = {
            str(name): {
                "display_name": str(details.get("display_name") or name),
                "language": str(details.get("language") or "unknown"),
                "environment": str(details.get("environment") or "not specified"),
            }
            for name, details in configured.items()
        }
        if not self.kernels:
            raise ValueError("At least one notebook kernel must be configured")

    def spec(self) -> ToolSpec:
        kernel_details = "; ".join(
            f"{name} (display_name={details['display_name']}, "
            f"language={details['language']}, environment={details['environment']})"
            for name, details in self.kernels.items()
        )
        return ToolSpec(
            name="executeCode",
            description=(
                    "Execute code in a Jupyter notebook cell using one of the configured "
                    "kernels. The kernels listed below were validated when BioPaster started. "
                    f"Configured kernels. Available kernels: {kernel_details}."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    # "script_path": {"type": "string",
                    #                 "description": "Path to the script file to execute, but can only use for files whose name starts with: BioPaster_evidence_."
                    #                 },
                    "markdown": {
                        "type": "string",
                        "description": (
                            "Optional Markdown content to save immediately before the code cell. "
                            "It is saved only when the code executes successfully."
                        ),
                    },
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 60},
                    "kernel": {
                        "type": "string",
                        "enum": list(self.kernels.keys()),
                        "description": (
                            "Configured Jupyter kernel. Each kernel identifies "
                            "a specific language runtime and environment."
                        ),
                    },
                },
                "required": ["command", "kernel"],
                # "oneOf": [
                #     {"required": ["command", "language"],
                #      "not": {"required": ["script_path"]}},
                #     {"required": ["script_path", "language"],
                #      "not": {"required": ["command"]}},
                # ]
            },
            is_destructive=True,
            max_result_size_chars=20_000,
        )
        
    def prepare_input(self, tool_input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        unknown = set(tool_input) - {"kernel", "command", "markdown", "cwd", "timeout_s"}
        if unknown:
            raise ToolInputError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        # if ("command" in tool_input) == ("script_path" in tool_input):
        #     raise ToolInputError("Provide exactly one of command or script_path.")
        if not isinstance(tool_input.get("kernel"), str) or not tool_input["kernel"]:
            raise ToolInputError("kernel must be a non-empty string")
        for key in ("command", "cwd"):
            if key in tool_input and (not isinstance(tool_input[key], str) or not tool_input[key].strip()):
                raise ToolInputError(f"{key} must be a non-empty string")
        kernel = tool_input["kernel"]
        if kernel not in self.kernels:
            available = ", ".join(self.kernels)
            raise ToolInputError(
                f"Unsupported kernel: {kernel}. Available kernels: {available}"
            )
        timeout = tool_input.get("timeout_s", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ToolInputError("timeout_s must be a positive integer")
        if context.notebook_path is None:
            raise ToolInputError("notebook_path is not configured")
        cwd_paths = context.resolve_permission_paths(tool_input.get("cwd") or context.cwd)
        if not cwd_paths[-1].is_dir():
            raise ToolInputError(f"cwd is not a directory: {cwd_paths[-1]}")
        notebook_paths = context.resolve_permission_paths(context.notebook_path)
        if any(p.name.startswith("BioPaster_evidence_") for p in notebook_paths):
            raise ToolPermissionError("Notebook output cannot overwrite evidence files.")
        notebook_targets = tuple(PermissionTarget("Edit", str(p)) for p in notebook_paths)
        notebook_path = notebook_paths[-1]
        command = tool_input.get("command")
        markdown = tool_input.get("markdown")
        #m if "script_path" in tool_input:
        #     script = tool_input["script_path"]
        #     if not script.strip():
        #         raise ToolInputError("script_path cannot be empty")
        #     paths = context.resolve_permission_paths(script)
        #     if not paths[-1].is_file():
        #         raise ToolInputError(f"Script is not a regular file: {paths[-1]}")
        #     # Reading and executing are separate approvals. Read exactly once.
        #     context.permission_context.authorize(PermissionRequest(
        #         self.spec().name, context.active_tool_use_id, f"Read script: {paths[-1]}",
        #         tuple(PermissionTarget("Read", str(p)) for p in paths),
        #         tuple(PermissionRule("Read", str(p)) for p in paths),
        #     ))
        #     command = _script_parse(paths[-1])
        if not isinstance(command, str) or not command.strip():
            raise ToolInputError("Code cannot be empty")
        context.permission_context.check_command_restrictions(command)
        if markdown is not None:
            if not isinstance(markdown, str) or not markdown.strip():
                raise ToolInputError("markdown must be a non-empty string")
            if COPIED_CONTENT_HEADER in markdown:
                raise ToolInputError(
                    "markdown must not contain the Copied Content header"
                )
            if markdown.count(REFERENCE_HEADER) > 1:
                raise ToolInputError(
                    "markdown must contain at most one Reference section"
                )

        return {"command": command, "kernel_name": kernel,
                "kernel_language": self.kernels[kernel]["language"],
                "markdown": markdown,
                "kernel_environment": self.kernels[kernel]["environment"],
                "timeout_s": timeout, "cwd": str(cwd_paths[-1]),
                "notebook_path": str(notebook_path), "_cwd_paths": cwd_paths,
                "_notebook_targets": notebook_targets}

    def check_permissions(self, tool_input, context):
        name = self.spec().name
        targets = (PermissionTarget(name, None),)
        targets += tuple(PermissionTarget("Read", str(p)) for p in tool_input["_cwd_paths"])
        targets += tool_input["_notebook_targets"]
        markdown = tool_input.get("markdown")
        markdown_description = (
            f"Markdown:\n{markdown}\n"
            if markdown
            else ""
        )
        return PermissionRequest(
            name, None,
            f"Kernel: {tool_input['kernel_name']}\n"
            f"Language: {tool_input['kernel_language']}\n"
            f"Environment: {tool_input['kernel_environment']}\n"
            f"Working directory: {tool_input['cwd']}\n"
            f"Notebook: {tool_input['notebook_path']}\n"
            f"{markdown_description}"
            f"Code:\n{tool_input['command']}",
            targets, (PermissionRule(name),),
        )

    def run(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        command = tool_input["command"]
        markdown = tool_input.get("markdown")
        timeout_s = tool_input["timeout_s"]
        kernel_name = tool_input["kernel_name"]
        cwd = tool_input["cwd"]
        notebook_path = Path(tool_input["notebook_path"])

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

        cells_to_save = []
        if markdown:
            cells_to_save.append(
                nbformat.v4.new_markdown_cell(markdown)
            )
        cells_to_save.append(notebook.cells[0])
        try:
            _save_cells(
                cells=cells_to_save,
                notebook_path=notebook_path,
                kernel_name=kernel_name,
            )
            tool_outputs.append({
            "type": "text",
            "content": f"Executed successfully, and it was saved to: {notebook_path}",
            "metadata": {"notebook_path": str(notebook_path)},
            })

            return ToolResult(
                name="executeCode",
                output=tool_outputs,
                is_error=False,
            )
        except Exception as e:
            tool_outputs.append({
            "type": "text",
            "content": f"[error] failed to save notebook: {e}",
            "metadata": {"notebook_path": str(notebook_path)},
            })
            return ToolResult(
                name="executeCode",
                output=tool_outputs,
                is_error=True,
            )

        
