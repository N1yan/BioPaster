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
    
    
    def to_dict(self) -> dict:
        """Serialize conversation messages without changing their content."""
        messages = []

        for message in self.messages:
            if isinstance(message.content, str):
                content = message.content
            else:
                content = []

                for block in message.content:
                    if isinstance(block, TextContentBlock):
                        content.append({
                            "type": "text",
                            "text": block.text,
                        })
                    elif isinstance(block, ToolUseContentBlock):
                        content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    elif isinstance(block, ToolResultContentBlock):
                        content.append({
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        })
                    else:
                        raise ValueError(
                            f"Unsupported content block: {type(block).__name__}"
                        )
                        
            messages.append({
                "role": message.role,
                "content": content,
                "timestamp": message.timestamp,
                "_is_internal": message._is_internal,
            })

        return {"messages": messages}
    
    
    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        """Restore a conversation from serialized messages."""
        if not isinstance(data, dict):
            raise ValueError("Conversation must be an object.")

        items = data.get("messages")
        if not isinstance(items, list):
            raise ValueError("Conversation messages must be a list.")

        messages = []

        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each message must be an object.")

            role = item.get("role")
            if role not in ("user", "assistant", "system"):
                raise ValueError(f"Invalid message role: {role!r}")

            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str):
                raise ValueError("Message timestamp must be a string.")

            internal = item.get("_is_internal", False)
            if not isinstance(internal, bool):
                raise ValueError("_is_internal must be a boolean.")

            raw_content = item.get("content")

            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                content = []

                for block in raw_content:
                    if not isinstance(block, dict):
                        raise ValueError("Content blocks must be objects.")

                    block_type = block.get("type")

                    if block_type == "text":
                        text = block.get("text")
                        if not isinstance(text, str):
                            raise ValueError("Text block must contain text.")

                        content.append(TextContentBlock(text=text))

                    elif block_type == "tool_use":
                        tool_id = block.get("id")
                        name = block.get("name")
                        tool_input = block.get("input")

                        if (
                            not isinstance(tool_id, str)
                            or not tool_id.strip()
                            or not isinstance(name, str)
                            or not name.strip()
                            or not isinstance(tool_input, dict)
                        ):
                            raise ValueError("Invalid tool_use block.")

                        content.append(ToolUseContentBlock(
                            id=tool_id,
                            name=name,
                            input=tool_input,
                        ))

                    elif block_type == "tool_result":
                        tool_id = block.get("tool_use_id")
                        result_content = block.get("content")
                        is_error = block.get("is_error")

                        if (
                            not isinstance(tool_id, str)
                            or not tool_id.strip()
                            or not isinstance(result_content, (str, list))
                            or not isinstance(is_error, bool)
                        ):
                            raise ValueError("Invalid tool_result block.")

                        if isinstance(result_content, list) and not all(
                            isinstance(part, dict)
                            for part in result_content
                        ):
                            raise ValueError(
                                "Tool result content must contain objects."
                            )

                        content.append(ToolResultContentBlock(
                            tool_use_id=tool_id,
                            content=result_content,
                            is_error=is_error,
                        ))
                    else:
                        raise ValueError(
                            f"Unsupported content block type: {block_type!r}"
                        )
            else:
                raise ValueError("Message content must be a string or list.")

            messages.append(Message(
                role=role,
                content=content,
                timestamp=timestamp,
                _is_internal=internal,
            ))

        return cls(messages=messages)
    
    
    def repair_pending_tool_results(self) -> list[str]:
        """Validate tool-result pairing and repair an unfinished final batch."""
        seen_ids: set[str] = set()
        pending: dict[str, str] = {}

        for message in self.messages:
            if message._is_internal:
                continue

            blocks = (
                message.content
                if isinstance(message.content, list)
                else []
            )
            calls = [
                block for block in blocks
                if isinstance(block, ToolUseContentBlock)
            ]
            results = [
                block for block in blocks
                if isinstance(block, ToolResultContentBlock)
            ]

            if calls and message.role != "assistant":
                raise ValueError(
                    "Tool calls must appear in assistant messages."
                )

            if results and message.role != "user":
                raise ValueError(
                    "Tool results must appear in user messages."
                )

            if pending:
                # While a batch is pending, only its result messages may follow.
                if not results or len(results) != len(blocks):
                    raise ValueError(
                        "Missing tool results before a later message."
                    )

            for result in results:
                if result.tool_use_id not in pending:
                    raise ValueError(
                        "Tool result has no pending call, or is duplicated: "
                        f"{result.tool_use_id}"
                    )

                del pending[result.tool_use_id]

            for call in calls:
                if call.id in seen_ids:
                    raise ValueError(
                        f"Duplicate tool call ID: {call.id}"
                    )

                seen_ids.add(call.id)
                pending[call.id] = call.name

        # Only a missing final batch is repairable.
        repaired_ids = list(pending)

        for tool_id, tool_name in pending.items():
            self.add_tool_result_message(
                tool_use_id=tool_id,
                content=(
                    f"No saved result is available for tool {tool_name}. "
                    "The previous execution was interrupted or its result "
                    "was not saved. Execution status is unknown. Verify "
                    "the current state before deciding whether to retry."
                ),
                is_error=True,
            )

        return repaired_ids