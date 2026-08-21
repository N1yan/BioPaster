from tool_system.tools import (
    WebSearchTool,
    WebFetchTool,
    PaperSearchTool,
    PaperFetchTool,
    FileDownloadTool,
    ReadTool,
    CopyPasteTool,
)
from tool_system.registry import ToolRegistry

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
        ]
    )
    return registry