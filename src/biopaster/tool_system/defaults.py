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

def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry(
        tools=[
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
            ExecuteCodeTool(),
            BashTool(),
        ]
    )
    return registry
