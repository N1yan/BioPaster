"""Conversation management"""

from dataclasses import dataclass, field
from typing import Any, Union
from datetime import datetime

@dataclass
class TextContentBlock:
    """A text content block."""
    type: str = "text"
    text: str = ""

@dataclass
class ToolUseContentBlock:
    """A tool use content block."""
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    
@dataclass
class ToolResultContentBlock:
    """A tool result content block."""
    type: str = "tool_result"
    tool_use_id: str = ""
    content: Union[str, list[dict[str, Any]]] = ""
    is_error: bool = False

ContentBlock = Union[TextContentBlock, ToolUseContentBlock, ToolResultContentBlock]

@dataclass
class Message:
    """Conversation message."""
    role: str  # "user", "assistant", "system"
    content: Union[str, list[ContentBlock]]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    # Internal marker for messages that should be filtered from API (e.g., compact boundary)
    _is_internal: bool = field(default=False, repr=False)

    
@dataclass
class Conversation:
    """Conversation manager."""
    messages: list[Message] = field(default_factory=list)
    
    def add_message(self, role: str, content: Union[str, list[ContentBlock]]):
        self.messages.append(Message(role=role, content=content))
        
    def add_assistant_message(self, content: Union[str, list[ContentBlock]]):
        self.add_message(role="assistant", content=content)
        
    def add_tool_result_message(self, tool_use_id: str, content: Union[str, list[dict]], is_error: bool = False):
        """Add a tool result message."""
        block = ToolResultContentBlock(
            type="tool_result",
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error
        )
        self.add_message("user", [block])
        
    def add_user_message(self, text: str):
        self.add_message(role="user", content=text)
    
    def _is_tool_result_message(self, api_message: dict) -> bool:
        content = api_message["content"]
        return (
            api_message["role"] == "user"
            and isinstance(content, list)
            and bool(content)
            and all(
                block.get("type") == "tool_result"
                for block in content
            )
        )

    def get_messages(self) -> list[dict]:
        """Get messages in API format (Anthropic style)."""
         
        api_messages = []
        for msg in self.messages:
            if getattr(msg,  "_is_internal", False):
                continue
            if isinstance(msg.content, str):
                api_messages.append({"role": msg.role, "content": msg.content})
            else:
                content_blocks = []
                for block in msg.content:
                    if isinstance(block, TextContentBlock):
                        content_blocks.append({"type": "text", "text": block.text})
                    elif isinstance(block, ToolUseContentBlock):
                        content_blocks.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input
                        })
                    elif isinstance(block, ToolResultContentBlock):
                        content_blocks.append({
                            "type":  "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error
                        })
                api_messages.append({"role": msg.role, "content": content_blocks})
                
        # merge tool results in one turn into a single user message
        merged_api_messages = []
        for message in api_messages:
            if (
                merged_api_messages
                and self._is_tool_result_message(message)
                and self._is_tool_result_message(merged_api_messages[-1])
            ):
                merged_api_messages[-1]["content"].extend(message["content"])
            else:
                merged_api_messages.append(message)

        return merged_api_messages
    