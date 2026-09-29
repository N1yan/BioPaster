import re
import json
from biopaster.tool_system.registry import ToolSpec
from mcp.types import Tool as McpTool, CallToolResult
from mcp import ClientSession
from biopaster.tool_system.protocol import ToolResult
from .result import convert_mcp_result
from jsonschema import validate, ValidationError
from biopaster.tool_system.errors import ToolInputError
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.permissions import (
    PermissionRequest,
    PermissionTarget,
    PermissionRule,
)
from anyio.from_thread import BlockingPortal
from biopaster.tool_system.errors import ToolExecutionError



def build_mcp_tool_spec(
    server_name: str,
    mcp_tool: McpTool,
) -> ToolSpec:
    tool_name = f"mcp__{server_name}__{mcp_tool.name}"
    
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool_name):
        raise ValueError(f"Unsupported tool name: {tool_name}")
    
    return ToolSpec(
        name=tool_name,
        description=mcp_tool.description or "",
        input_schema=mcp_tool.inputSchema
    )
    
class McpToolBinding:
    def __init__(
        self,
        server_name: str,
        mcp_tool: McpTool,
        session: ClientSession,
        portal: BlockingPortal | None = None,
        call_timeout_seconds: float = 60.0,
    ):
        self.server_name = server_name
        self.original_tool_name = mcp_tool.name
        self.session = session
        self.tool_spec = build_mcp_tool_spec(server_name, mcp_tool)
        self.portal = portal
        if call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be positive")
        self.call_timeout_seconds = call_timeout_seconds
        
    def validate_arguments(self, arguments: dict) -> None:
        if not isinstance(arguments, dict):
            raise ToolInputError("MCP tool arguments must be an object")
        
        try:
            validate(
                instance=arguments,
                schema=self.tool_spec.input_schema
            )
        except ValidationError as e:
            raise ToolInputError(
                f"{self.tool_spec.name}: {e.message}"
            ) from e
        
    async def call(self, arguments: dict) -> ToolResult:
        self.validate_arguments(arguments)
        mcp_result =  await self.session.call_tool(
            self.original_tool_name,
            arguments=arguments
        )
        
        return convert_mcp_result(
            mcp_result,
            tool_name=self.tool_spec.name,
            server_name=self.server_name)
        
    def spec(self) -> ToolSpec:
        return self.tool_spec
    
    def prepare_input(
        self,
        tool_input: dict,
        context: ToolContext,
    ) -> dict:
        self.validate_arguments(tool_input)
        return tool_input.copy()
        
    def check_permissions(
        self,
        arguments: dict,
        context: ToolContext
    ) -> PermissionRequest:
        tool_name = self.tool_spec.name
        
        return PermissionRequest(
            tool_name=tool_name,
            tool_use_id=context.active_tool_use_id,
            description=(
                f"Call MCP server: {self.server_name}\n"
                f"Tool: {self.original_tool_name}\n"
                f"Arguments: {json.dumps(arguments, ensure_ascii=False)}"
            ),
            targets=(
                 PermissionTarget(
                    tool_name=tool_name,
                    rule_content=None,
                ),
            ),
            suggestions=(
                PermissionRule(tool_name=tool_name),
            )
        )
        
    def run(
        self,
        tool_input: dict,
        context: ToolContext,
    ) -> ToolResult:
        if self.portal is None:
            raise ToolExecutionError(
                "Synchronous MCP execution requires a BlockingPortal."
            )

        pending_call = self.portal.start_task_soon(
            self.call,
            tool_input
        )
        
        try:
            return pending_call.result(
                timeout=self.call_timeout_seconds
            )
        except TimeoutError as e:
            pending_call.cancel()
            raise ToolExecutionError(
                f"MCP call timed out: {self.tool_spec.name}. "
                "The remote operation may still be running."
            ) from e
        except BaseException:
            pending_call.cancel()
            raise