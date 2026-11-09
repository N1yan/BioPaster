from dataclasses import dataclass
from typing import Any, Optional, TypeAlias

@dataclass
class ChatMessage:
    """Represents a chat message."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary."""
        return {"role": self.role, "content": self.content}

MessageInput: TypeAlias = ChatMessage | dict[str, Any]

@dataclass
class ChatResponse:
    """Represents a chat response."""
    
    content: str
    model: str
    usage: dict[str, Any]
    finish_reason: str
    reasoning_content: Optional[str] = None
    tool_uses: Optional[list[dict[str, Any]]] = None


class ToolCallFormatError(ValueError):
    """A completed model response contains unusable tool-call structure."""

    def __init__(self, message: str, *, response: ChatResponse, details: Any = None):
        super().__init__(message)
        self.response = response
        self.details = details


def validate_tool_calls(response: ChatResponse, *, details: Any = None) -> ChatResponse:
    """Validate the whole batch before exposing any executable calls."""
    calls = response.tool_uses if response.tool_uses is not None else []

    def invalid(message):
        raise ToolCallFormatError(message, response=response,
                                  details=calls if details is None else details)

    if not isinstance(calls, list):
        invalid("Tool calls must be a list.")

    if response.finish_reason == "tool_use" and not calls:
        invalid("The response ended with tool_use but contained no tool calls.")
    if calls and response.finish_reason != "tool_use":
        invalid("The response contained tool calls but did not end with tool_use.")
    seen_ids = set()
    for call in calls:
        if not isinstance(call, dict):
            invalid("Each tool call must be an object.")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            invalid("Each tool call requires a non-empty string ID.")
        if call_id in seen_ids:
            invalid(f"Duplicate tool call ID: {call_id}")
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            invalid("Each tool call requires a non-empty tool name.")
        if not isinstance(call.get("input"), dict):
            invalid(f"Arguments for tool {name!r} must be a JSON object.")
        seen_ids.add(call_id)
    return response
    

def prepare_messages(messages: list[MessageInput]) -> list[dict[str, Any]]:
    """Convert provider messages to API dictionary format."""
    return [msg if isinstance(msg, dict) else msg.to_dict() for msg in messages]
