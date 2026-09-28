from .tools import (
    WebSearchTool,
    WebFetchTool,
    PaperSearchTool,
    PaperFetchTool,
    FileDownloadTool,
    ReadTool,
    CopyPasteTool,
    GlobTool,
    WriteTool,
    EditTool,
    ExecuteCodeTool,
    BashTool,
)
from .registry import ToolRegistry
from typing import Any, Mapping

def build_default_registry(
    notebook_kernels: Mapping[str, Mapping[str, Any]] | None = None,
) -> ToolRegistry:
    tools = [
        WebSearchTool(),
        WebFetchTool(),
        PaperSearchTool(),
        PaperFetchTool(),
        FileDownloadTool(),
        ReadTool(),
        CopyPasteTool(),
        GlobTool(),
        WriteTool(),
        EditTool(),
    ]
    if notebook_kernels is None or notebook_kernels:
        tools.append(ExecuteCodeTool(notebook_kernels))
    tools.append(BashTool())

    registry = ToolRegistry(tools=tools)
    return registry
