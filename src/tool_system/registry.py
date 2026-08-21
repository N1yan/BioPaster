from dataclasses import dataclass
from typing import Any, Mapping, Iterable, Protocol
from tool_system.protocol import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    aliases: tuple[str, ...] = ()
    is_read_only: bool = False
    is_destructive: bool = False
    strict: bool = False
    max_result_size_chars: int = 20_000


class Tool(Protocol):
    def spec(self) -> ToolSpec: ...
    def run(self, tool_input: dict[str, Any]) -> ToolResult: ...
    
    
class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: list[Tool] = []
        self._by_name: dict[str, Tool] = {}
        if tools:
            for tool in tools:
                self.register(tool)
    
    def register(self, tool: Tool) -> None:
        spec = tool.spec()
        key = spec.name.lower()
        if key in self._by_name:
            raise ValueError(f"Tool with name {spec.name} already registered")
        self._tools.append(tool)
        self._by_name[key] = tool
        for alias in spec.aliases:
            alias_key = alias.lower()
            if alias_key in self._by_name:
                raise ValueError(f"duplicate tool alias: {alias}")
            self._by_name[alias_key] = tool
            
    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools]
    
    def dispatch(self, call) -> ToolResult:
        tool_name = call.name
        tool_input = call.input
        if tool_name.lower() not in self._by_name.keys():
            return ToolResult(
                name=tool_name,
                output=[{
                    "type": "text",
                    "content": f"Tool {tool_name} not found",
                }],
                is_error=True
            )
        tool = self._by_name[tool_name.lower()]
        try:
            return tool.run(tool_input)
        except Exception as e:
            return ToolResult(
                name=tool_name,
                output=[{
                    "type": "text",
                    "content": f"Tool {tool_name} failed: {e}"
                }],
                is_error=True
            )