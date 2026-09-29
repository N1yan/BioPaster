from mcp.types import CallToolResult
from biopaster.tool_system.protocol import ToolResult


def convert_mcp_result(
    mcp_result: CallToolResult,
    *,
    tool_name: str,
    server_name: str,
) -> ToolResult:
    outputs = []
    
    for block in mcp_result.content:
        if block.type == "text":
            outputs.append({
                "type": "text",
                "content": block.text,
                "metadata": {"mcp_server": server_name}
            })
        elif block.type == "image":
            outputs.append({
                "type": "image",
                "content": block.data,
                "metadata": {
                    "mcp_server": server_name,
                    "media_type": block.mimeType,
                },
            })
        else:
            raise ValueError(f"Unsupported MCP content type: {block.type}")

    if mcp_result.structuredContent is not None:
        outputs.append({
            "type":"text",
            "content": mcp_result.structuredContent,
            "metadata": {
                "mcp_server": server_name,
                "mcp_content_kind": "structured",
            },
        })
        
    return ToolResult(
        name=tool_name,
        output=outputs,
        is_error=mcp_result.isError
    )