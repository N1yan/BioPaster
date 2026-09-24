from dataclasses import dataclass
from typing import Any, Literal, Optional, TypedDict


class ToolOutput(TypedDict):
    type: Literal["text", "image"]
    content: str | list[Any] | dict[str, Any]
    metadata: dict[str, Any]


@dataclass
class ToolResult:
    name: str
    output: list[ToolOutput]
    is_error: bool = False
    tool_use_id: Optional[str] = None
    
@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    tool_use_id: Optional[str] = None
