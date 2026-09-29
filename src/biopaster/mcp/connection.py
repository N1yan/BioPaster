from mcp import StdioServerParameters, ClientSession
from mcp.client.stdio import stdio_client
from contextlib import asynccontextmanager, AsyncExitStack
import httpx
from mcp.client.streamable_http import streamable_http_client
from .config import (
    StdioServerConfig,
    HttpServerConfig,
    McpServerConfig,
)
from anyio import fail_after


@asynccontextmanager
async def connect_stdio(server_config: StdioServerConfig):
    server_parameters = StdioServerParameters(
        command=server_config.command,
        args=server_config.args,
        env=server_config.env,
        cwd=server_config.cwd
    )
    
    async with stdio_client(server_parameters) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            with fail_after(server_config.connect_timeout_seconds):
                await session.initialize()
            yield session
                      
@asynccontextmanager
async def connect_http(server_config: HttpServerConfig):
    async with httpx.AsyncClient(
        headers=server_config.headers,
        timeout=httpx.Timeout(30.0, read=300.0),
    ) as http_client:
        async with streamable_http_client(
            server_config.url,
            http_client=http_client
        ) as streams:
            read_stream = streams[0]
            write_stream = streams[1]
            
            async with ClientSession(
                read_stream,
                write_stream
            ) as session:
                with fail_after(server_config.connect_timeout_seconds):
                    await session.initialize()
                yield session
                
@asynccontextmanager
async def connect_mcp(server_config: McpServerConfig):
    if isinstance(server_config, StdioServerConfig):
        connection = connect_stdio(server_config)

    elif isinstance(server_config, HttpServerConfig):
        connection = connect_http(server_config)

    else:
        raise TypeError(
            f"Unsupported MCP config type: {type(server_config).__name__}"
        )

    async with connection as session:
        yield session

@asynccontextmanager
async def connect_mcp_servers(server_configs: dict[str, McpServerConfig]):
    sessions: dict[str, ClientSession] = {}
    
    async with AsyncExitStack() as connection_stack:
        for server_name, server_config in server_configs.items():
            session = await connection_stack.enter_async_context(
                connect_mcp(server_config)
            )
            sessions[server_name] = session
            
        yield sessions