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
    

def prepare_messages(messages: list[MessageInput]) -> list[dict[str, Any]]:
    """Convert provider messages to API dictionary format."""
    return [msg if isinstance(msg, dict) else msg.to_dict() for msg in messages]