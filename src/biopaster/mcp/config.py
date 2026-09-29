import os
import re
from dataclasses import dataclass, field


@dataclass
class StdioServerConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None
    connect_timeout_seconds: float = 30.0
    
@dataclass
class HttpServerConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    connect_timeout_seconds: float = 30.0
    
McpServerConfig = StdioServerConfig | HttpServerConfig

@dataclass
class McpConfigResult:
    servers: dict[str, McpServerConfig] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    disabled_servers: list[str] = field(default_factory=list)
    
def expand_env_variables(config_value):
    if isinstance(config_value, str):
        def replace_variable(match):
            variable_name = match.group(1)

            if variable_name not in os.environ:
                raise ValueError(
                    f"Missing environment variable: {variable_name}"
                )

            return os.environ[variable_name]

        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            replace_variable,
            config_value,
        )

    if isinstance(config_value, list):
        return [
            expand_env_variables(item)
            for item in config_value
        ]

    if isinstance(config_value, dict):
        return {
            key: expand_env_variables(value)
            for key, value in config_value.items()
        }

    return config_value

def parse_server_config(server_config: dict) -> McpServerConfig:
    if not isinstance(server_config, dict):
        raise ValueError("MCP server config must be an object")
    
    options = server_config.copy()
    transport_type = options.pop("type", "stdio")
    
    for field_name in ("command", "args", "env", "cwd", "url", "headers"):
        if field_name in options:
            options[field_name] = expand_env_variables(options[field_name])

    if transport_type == "stdio":
        command = options.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("stdio server requires a non-empty command")
        
        return StdioServerConfig(**options)
    
    if transport_type in {"http", "streamable-http"}:
        url = options.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("HTTP server requires a non-empty url")

        return HttpServerConfig(**options)
    
    raise ValueError(f"Unsupported MCP transport: {transport_type}")


def parse_mcp_servers(app_config: dict) -> McpConfigResult:
    server_configs = app_config.get("mcpServers", {})

    if not isinstance(server_configs, dict):
        raise ValueError("mcpServers must be an object")

    result = McpConfigResult()

    for server_name, server_config in server_configs.items():
        try:
            if not isinstance(server_name, str) or not server_name.strip():
                raise ValueError(
                    "MCP server name must be a non-empty string"
                )

            if not isinstance(server_config, dict):
                raise ValueError("MCP server config must be an object")

            options = server_config.copy()
            enabled = options.pop("enabled", True)

            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false")

            if not enabled:
                result.disabled_servers.append(server_name)
                continue

            result.servers[server_name] = parse_server_config(options)

        except (TypeError, ValueError) as error:
            result.errors[str(server_name)] = str(error)

    return result