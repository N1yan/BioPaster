import re
from ..agent.conversation import Conversation, Message
from dataclasses import dataclass
import json
import math
from typing import Any


COMPACT_SYSTEM_PROMPT = """
You summarize an agent conversation so that the work can continue
without access to the original messages.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
"""


COMPACT_PROMPT = """
Create a detailed summary of the conversation so far.

Preserve:

1. The user's requests and intent.
2. Important technical decisions.
3. Files, functions and code involved.
4. Errors encountered and their fixes.
5. Work already completed.
6. Pending work.
7. Current work and next step.
8. Important user corrections and preferences.
9. Input data paths and output paths.
10. Any other information that is important to continue the work.

Respond in this format:

<summary>
Detailed summary here.
</summary>
"""

MAX_CONSECUTIVE_FAILURES = 3
MAX_SUMMARY_TOKENS = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000

@dataclass
class AutoCompactState:
    consecutive_failures: int = 0

    
@dataclass
class AutoCompactResult:
    messages: list[Message]
    was_compacted: bool
    pre_compact_tokens: int = 0
    summary_tokens: int = 0
    error: str | None = None

def _extract_summary(text) -> str:
    match = re.search(
        r"<summary>(.*?)</summary>",
        text,
        flags=re.DOTALL,
    )

    if match:
        return match.group(1).strip()

    return text.strip()

def _generate_compact_summary(
    messages: list[dict[str, Any]],
    provider,
    max_tokens: int
    ) -> tuple[str, int]:
    
    messages.append({"role": "user", "content": COMPACT_PROMPT})
    
    response = provider.chat_stream_response(
        messages,
        tools=None,
        system=COMPACT_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        on_text_chunk=None,
    )
    
    if response.tool_uses:
        raise RuntimeError(
            "Auto compact model attempted to call a tool"
        )
    
    if response.finish_reason == "max_tokens":
        raise RuntimeError(
            "Auto compact summary was truncated because it reached max_tokens"
        )
    
    summary = _extract_summary(response.content or "")
    
    if not summary:
        raise RuntimeError(
            "Auto compact returned an empty summary"
        )

    output_tokens = (response.usage or {}).get(
        "output_tokens",
        0,
    )
    
    return summary, output_tokens

def _get_compact_threshold(
    context_window: int,
    model_max_output_tokens: int,
) -> int:
    
    summary_reserved = min(
        model_max_output_tokens,
        MAX_SUMMARY_TOKENS,
    )
    
    effective_window = context_window - summary_reserved
    threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS
    
    if threshold <= 0:
        raise ValueError(
            f"Context window: {context_window} is too small for the configured "
            f"summary reserve: {summary_reserved} and compact buffer: {AUTOCOMPACT_BUFFER_TOKENS}"
        )
    return threshold


def _estimate_input_tokens(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[dict[str, Any]],
) -> int:
    request_content = {
        "system": system_prompt,
        "tools": tools,
        "messages": messages,
    }

    encoded = json.dumps(
        request_content,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return max(1, math.ceil(len(encoded) / 3))

def auto_compact_messages(
    messages: list[Message],
    provider,
    *,
    context_window: int,
    model_max_output_tokens: int,
    state: AutoCompactState,
    **context_kwargs) -> AutoCompactResult:
    
    api_messages = Conversation(messages=messages).get_messages()
    input_tokens = _estimate_input_tokens(
        messages=api_messages,
        system_prompt=context_kwargs.get("system", ""),
        tools=context_kwargs.get("tool_schemas", [])
    )
    
    if state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        return AutoCompactResult(
            messages=messages,
            was_compacted=False,
            pre_compact_tokens=input_tokens,
            error=f"Auto compact disabled after {state.consecutive_failures} failures",
        )
    
    threshold = _get_compact_threshold(
        context_window=context_window,
        model_max_output_tokens=model_max_output_tokens,
    )

    if input_tokens < threshold:
        return AutoCompactResult(
                messages=messages,
                was_compacted=False,
                pre_compact_tokens=input_tokens,
            )
    
    try:
        summary, summary_tokens = _generate_compact_summary(
            messages=api_messages,
            provider=provider,
            max_tokens=min(
                model_max_output_tokens,
                MAX_SUMMARY_TOKENS,
            ),
        )
    except Exception as e:
        state.consecutive_failures += 1
        return AutoCompactResult(
            messages=messages,
            was_compacted=False,
            pre_compact_tokens=input_tokens,
            error=str(e),
        )

    
    state.consecutive_failures = 0
    
    summary_message = Message(
        role="user",
        content=(
            "[Compacted]\n"
            "The conversation before this point was compacted and summarized. "
            "Use the following summary to continue the work:\n\n"
            f"{summary}"
        ),
    )
    
    return AutoCompactResult(
        messages=[summary_message],
        was_compacted=True,
        pre_compact_tokens=input_tokens,
        summary_tokens=summary_tokens,
    )