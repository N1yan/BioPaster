from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolResult:
    name: str
    output: Any
    is_error: bool = False
    tool_use_id: Optional[str] = None
    
