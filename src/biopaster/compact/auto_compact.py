import re
from ..agent.conversation import Conversation, Message
from dataclasses import dataclass
import json
import math
from typing import Any
import time
import httpx


COMPACT_SYSTEM_PROMPT = """
You summarize an agent conversation so that the work can continue
without access to the original messages.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
"""


COMPACT_PROMPT = """
Create a detailed summary of the conversation so far, paying close attention
to the user's explicit requests and your previous actions. Preserve the
scientific context, data, methods, findings, evidence, and implementation
details needed to continue the work without access to the original messages.
Adapt the level of detail to the actual task, whether it involves literature
research, data analysis, writing, or software implementation.

Before providing the summary, organize your review in <analysis> tags:
1. Review the conversation chronologically. Identify the user's explicit
   requests, your approach, scientific and technical decisions, data used,
   methods, findings, supporting evidence, artifacts, errors, and fixes.
   Include file names, code snippets, function signatures, and file edits
   when they are needed to continue the work.
   Pay special attention to user feedback, especially requests to change
   your approach.
2. Double-check scientific and technical accuracy and completeness against
   the conversation. Preserve the recorded meaning of parameters, criteria,
   numerical results, and terminology. Do not add new explanations or
   silently correct the record using memory. Distinguish requested or
   planned work from executed work, observed results from interpretations,
   and established findings from hypotheses or unresolved uncertainties.

Automatically injected reminders may appear as user messages. Do not retain
or quote these reminders in the summary or list them as user requests.
Preserve actual user requests, corrections, and preferences.

Include the following sections in <summary>:

1. Primary Request and Intent
   Capture all of the user's explicit requests and intents in detail.
2. Scientific Context, Data, and Methods
   Preserve the research question and relevant biological or medical context.
   For data analysis, retain dataset identities, sample groups, comparisons,
   preprocessing, methods, parameters, selection criteria, and units as
   established in the conversation. Include data dimensions, field meanings,
   software versions, and technical decisions when needed to resume correctly.
3. Findings, Evidence, and Artifacts
   Preserve actual results, their associated methods and inputs, and relevant
   limitations or uncertainties. Retain the references, identifiers, links,
   and supporting passages needed to trace literature-based claims to the
   materials consulted. Do not present a planned analysis as a result or an
   unverified interpretation as an established finding.
   Enumerate relevant data files, reports, figures, notebooks, and code that
   were examined, modified, or created, explaining their role and status.
   Preserve exact input data paths and output paths. Include full code
   snippets and function signatures where needed to continue the work.
4. Errors and Fixes
   List errors encountered, how they were fixed, and relevant user feedback.
   Clearly identify errors that remain unresolved.
5. Problem Solving
   Describe solved problems and ongoing troubleshooting efforts.
6. User Messages
   List all actual user messages that are not tool results or automatically
   injected reminders, preserving feedback and changes in intent.
7. Pending Tasks
   List pending tasks the user explicitly requested.
8. Current Work
   Describe precisely what was being worked on immediately before this
   summary request. Pay particular attention to recent messages and include
   the relevant data, references, artifacts, and code where applicable.
9. Optional Next Step
   Identify the next step only when it directly follows the user's most
   recent explicit requests and the work underway before this summary.
   Do not revive completed or unrelated tasks. If there is a next step,
   quote the relevant recent conversation verbatim to establish the task
   and where work stopped. Do not quote automatically injected reminders.
10. Relevant Skills
    Preserve only skills needed for current or remaining work: exact names,
    SKILL.md paths, relevant arguments, and essential requirements or
    restrictions. Omit skills used solely for completed work unless still
    needed. Note when a skill must be reloaded for detailed instructions.

Preserve any other information essential to continuing the actual task.
Do not invent information to fill sections that do not apply.

Follow any additional summarization instructions provided in the conversation,
while excluding automatically injected reminders from the resulting summary.

Use this structure:
<analysis>
Review the conversation and check coverage and accuracy.
</analysis>
<summary>
1. Primary Request and Intent
...
2. Scientific Context, Data, and Methods
...
3. Findings, Evidence, and Artifacts
...
4. Errors and Fixes
...
5. Problem Solving
...
6. User Messages
...
7. Pending Tasks
...
8. Current Work
...
9. Optional Next Step
...
10. Relevant Skills
...
</summary>
"""

MAX_CONSECUTIVE_FAILURES = 3
MAX_SUMMARY_TOKENS = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MIN_AUTOCOMPACT_BUFFER_TOKENS = 1024

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


def _is_retryable_compact_error(error: Exception) -> bool:
    if isinstance(error, ExceptionGroup):
        return all(
            _is_retryable_compact_error(child)
            for child in error.exceptions
        )

    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return (
            status_code in {408, 409, 429}
            or 500 <= status_code < 600
        )

    return (
        isinstance(
            error,
            (
                TimeoutError,
                ConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ),
        )
        or type(error).__name__ in {
            "APIConnectionError",
            "APITimeoutError",
        }
    )

def _generate_compact_summary(
    messages: list[dict[str, Any]],
    provider,
    max_tokens: int
    ) -> tuple[str, int]:
    
    summary_messages = [
        *messages,
        {"role": "user", "content": COMPACT_PROMPT},
    ]

    response = provider.chat_stream_response(
        summary_messages,
        tools=None,
        system=COMPACT_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        on_text_chunk=None,
        max_retries=0,
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
    compact_buffer = min(
        AUTOCOMPACT_BUFFER_TOKENS,
        max(MIN_AUTOCOMPACT_BUFFER_TOKENS, context_window // 10),
    )
    threshold = (
        context_window
        - model_max_output_tokens
        - compact_buffer
    )

    if threshold <= 0:
        raise ValueError(
            f"Context window: {context_window} is too small for the configured "
            f"output reserve: {model_max_output_tokens} and compact buffer: {compact_buffer}"
        )
    return threshold


def _get_summary_max_tokens(
    context_window: int,
    model_max_output_tokens: int,
) -> int:
    return min(
        model_max_output_tokens,
        MAX_SUMMARY_TOKENS,
        max(1024, context_window // 4),
    )


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
    
    for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
        try:
            summary, summary_tokens = _generate_compact_summary(
                messages=api_messages,
                provider=provider,
                max_tokens=_get_summary_max_tokens(
                    context_window=context_window,
                    model_max_output_tokens=model_max_output_tokens,
                ),
            )
        except Exception as error:
            state.consecutive_failures += 1

            if (
                attempt == MAX_CONSECUTIVE_FAILURES
                or not _is_retryable_compact_error(error)
            ):
                return AutoCompactResult(
                    messages=messages,
                    was_compacted=False,
                    pre_compact_tokens=input_tokens,
                    error=f"{type(error).__name__}: {error}",
                )

            time.sleep(attempt)
        else:
            break

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
