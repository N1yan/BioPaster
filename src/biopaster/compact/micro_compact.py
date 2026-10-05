from ..agent.conversation import (
    Message,
    ToolUseContentBlock,
    ToolResultContentBlock,
)
from copy import deepcopy
from datetime import datetime

NON_COMPACTABLE_TOOLS = ["skill"] # Add tool names that should not be compacted, e.g., ["executecode"]
CLEARED_CONTENT = "[Old tool result content cleared]"

def _idle_time_calculate(
    messages: list[Message], 
    now: datetime | None = None
    ) -> float:
    """
    Calculate the idle time in minutes since the last assistant message.
    """
    last_assistant = next((message for message in reversed(messages) if message.role == "assistant"), None)
    if last_assistant is None:
        return 0.0
    
    try:
        last_assistant_time = datetime.fromisoformat(last_assistant.timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0

    if now is None:
        if last_assistant_time.tzinfo is None:
            now = datetime.now()
        else:
            now = datetime.now(last_assistant_time.tzinfo)
    
    return (now - last_assistant_time).total_seconds() / 60

def micro_compact_messages(
    messages: list[Message], 
    keep_recent: int = 5,
    cache_ttl_minutes: int = 60,
    now: datetime | None = None
    )-> list[Message]:
    """
    Compact a list of messages by clearing the content of older tool results.
    """
    if not messages:
        return messages

    idle_minutes = _idle_time_calculate(messages, now)
    if idle_minutes < cache_ttl_minutes:
        return messages
    
    excluded_tools = {
        name.casefold()
        for name in NON_COMPACTABLE_TOOLS
    }

    compactable_ids: list[str] = []
    for msg in messages:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseContentBlock) and block.name.casefold()  not in excluded_tools:
                compactable_ids.append(block.id)
    
    keep_recent = max(1, keep_recent)
    clear_ids = set(compactable_ids[:-keep_recent])
    
    compacted = deepcopy(messages)
    
    for msg in compacted:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if (isinstance(block, ToolResultContentBlock) 
                and block.tool_use_id in clear_ids
                and block.content != CLEARED_CONTENT
                ):
                block.content = CLEARED_CONTENT
                
    return compacted
