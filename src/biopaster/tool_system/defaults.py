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
    SkillTool,
)
from .registry import Tool, ToolRegistry
from typing import Any, Iterable, Mapping

def build_default_registry(
    notebook_kernels: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    extra_tools: Iterable[Tool] = (),
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
        SkillTool(),
    ]
    if notebook_kernels is None or notebook_kernels:
        tools.append(ExecuteCodeTool(notebook_kernels))
    tools.append(BashTool())

    registry = ToolRegistry(tools=tools)
    for tool in extra_tools:
        registry.register(tool)
    return registry
