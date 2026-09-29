from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from anyio.from_thread import start_blocking_portal
from .config import McpServerConfig
from .connection import connect_mcp
from .tool import McpToolBinding


@dataclass
class McpStartupResult:
    tools: dict[str, McpToolBinding] = field(default_factory=dict)
    connected_servers: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

@contextmanager
def open_mcp_tools(
    server_configs: dict[str, McpServerConfig],
    *,
    call_timeout_seconds: float = 60.0,
):
    startup_result = McpStartupResult()

    if not server_configs:
        yield startup_result
        return

    with start_blocking_portal() as portal:
        with ExitStack() as active_connections:
            for server_name, server_config in server_configs.items():
                try:
                    with ExitStack() as pending_connection:
                        session = pending_connection.enter_context(
                            portal.wrap_async_context_manager(
                                connect_mcp(server_config)
                            )
                        )

                        server_tools = {}
                        cursor = None

                        while True:
                            pending_discovery = portal.start_task_soon(
                                session.list_tools,
                                cursor,
                            )
                            
                            try:
                                tool_page = pending_discovery.result(
                                    timeout=server_config.connect_timeout_seconds,
                                )
                            except BaseException:
                                pending_discovery.cancel()
                                raise

                            for mcp_tool in tool_page.tools:
                                binding = McpToolBinding(
                                    server_name=server_name,
                                    mcp_tool=mcp_tool,
                                    session=session,
                                    portal=portal,
                                    call_timeout_seconds=call_timeout_seconds,
                                )
                                tool_key = binding.spec().name.lower()
                                if (
                                    tool_key in server_tools
                                    or tool_key in startup_result.tools
                                ):
                                    raise ValueError(
                                        f"Duplicate MCP tool name: {tool_key}"
                                    )
                                server_tools[tool_key] = binding

                            cursor = tool_page.nextCursor
                            if cursor is None:
                                break

                        active_connections.enter_context(
                            pending_connection.pop_all()
                        )

                    startup_result.tools.update(server_tools)
                    startup_result.connected_servers.append(server_name)

                except Exception as e:
                    while isinstance(e, BaseExceptionGroup):
                        e = e.exceptions[0]

                    startup_result.errors[server_name] = type(e).__name__

            yield startup_result