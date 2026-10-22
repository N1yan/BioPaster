
import json
from typing import Any, Callable
from ..providers.base import ChatResponse
from dataclasses import dataclass, field
from ..tool_system.registry import ToolRegistry
from .conversation import Conversation, TextContentBlock, ToolUseContentBlock
from ..tool_system.context import ToolContext
from .prompts import assemble_system_prompt, TUTORIAL_REMINDER
from .task_context import format_task_context
from ..tool_system.protocol import ToolResult, ToolCall
from ..tool_system.persist import Persist
from ..tool_system.map_result import map_tool_result
from ..compact.micro_compact import micro_compact_messages
from ..compact.auto_compact import (
    auto_compact_messages,
    AutoCompactState,
)

@dataclass(frozen=True)
class ToolEvent:
    kind: str
    tool_name: str
    tool_input: dict[str, Any] | None = None
    tool_output: Any | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    error: str | None = None

@dataclass(frozen=True)
class AgentLoopResult:
    """Result of running the agent loop."""
    response_text: str
    usage: dict[str, Any] | None = None  # {"input_tokens": int, "output_tokens": int}
    num_turns: int = 0
    subtype: str = "success"
    errors: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class ResultEvent:
    subtype: str
    is_error: bool
    result: str
    errors: list[str]
    num_turns: int
    type: str = "result"
    
def _safe_call_handler(handler: Callable | None, event: ToolEvent | ResultEvent):
    if handler is None:
        return
    handler(event)

# ── Tool Result Persist ──
def _process_tool_result(
    tool_result: ToolResult,
    tool_use_id: str,
    context: ToolContext,
):
    if context.session_log is None:
        raise RuntimeError("A session is required to persist tool results.")

    result_store = Persist(
        context.session_log.directory / ".tool_results",
        max_content_length=20000,
    )

    tool_result = result_store.persist(tool_result, tool_use_id)

    return map_tool_result(
        tool_result,
        max_text_chars=result_store.max_content_length,
    )

def _emit_text_chunks(handler: Callable | None, text: str, *, chunk_size: int = 12) -> None:
    """Emit text in small chunks for user-visible streaming without changing loop semantics."""
    if handler is None or not text:
        return
    if chunk_size <= 0:
        chunk_size = len(text)
    for idx in range(0, len(text), chunk_size):
        try:
            handler(text[idx: idx + chunk_size])
        except Exception:
            return
        
def _call_provider_for_turn(
    *,
    provider,
    api_messages: list[dict[str, Any]],
    call_kwargs: dict[str, Any],
    stream: bool = True,
    on_text_chunk: Callable | None,
    ) -> tuple[Any, bool]:
    # if stream:
    try:
        response = provider.chat_stream_response(
            api_messages,
            on_text_chunk=on_text_chunk,
            **call_kwargs,
        )
        if not isinstance(response, ChatResponse):
            raise TypeError("Structured streaming must return ChatResponse")
        return response, stream
    except NotImplementedError:
        pass
    except Exception:
        raise

def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> str:
    return

def finish(
    text: str,
    subtype: str = "success",
    errors: list[str] | None = None,
    on_event: Callable | None = None,
    turn_count: int = 0,
    total_usage: dict | None = None
) -> AgentLoopResult:
    _safe_call_handler(
        on_event,
        ResultEvent(
            subtype=subtype,
            is_error=subtype != "success",
            result=text,
            errors=errors if errors is not None else [],
            num_turns=turn_count,
        ),
    )

    return AgentLoopResult(
        response_text=text,
        usage=total_usage if total_usage and any(total_usage.values()) else None,
        num_turns=turn_count,
        subtype=subtype,
        errors=errors or [],
    )

# ── Agent Loop ──
def agent_loop(
    conversation: Conversation,
    provider,
    tool_registry: ToolRegistry,
    tool_context: ToolContext,
    max_turns: int = 500,
    stream: bool = True,
    on_text_chunk: Callable | None = None,
    on_event: Callable | None = None,
    on_checkpoint: Callable[[], None] | None = None,
    )-> AgentLoopResult:
    """Run agent loop: LLM -> tools -> LLM until no more tools or max turns.
    Returns:
        AgentLoopResult with final text response, usage info, and turn count
    """
    
    detector = conversation.loop_detector
    detector.begin_task()

    def finish_run(**kwargs):
        detector.status = kwargs.get("subtype", "success")
        if kwargs.get("errors"):
            detector.diagnostic = "\n".join(kwargs["errors"])
        if on_checkpoint is not None:
            on_checkpoint()
        return finish(**kwargs)

    # system prompt
    system_prompt = assemble_system_prompt(tool_context)

    # Convert tools to schemas (Anthropic format)
    tool_schemas = []
    for spec in tool_registry.list_specs():
        tool_schemas.append({
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        })
        
    last_user_visible_message: str | None = None
    
    # ── content compact ──
    auto_compact_state = AutoCompactState(
        consecutive_failures=0
    )
    
    total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    turn_count = 0
    for turn in range(max_turns):
        call_kwargs: dict[str, Any] = {
            "tools": tool_schemas,
            "system": system_prompt,
            "max_tokens": provider.max_output_tokens,
        }
        
        # compact messages
        conversation.messages = micro_compact_messages(conversation.messages, 
                                                       keep_recent=5, 
                                                       cache_ttl_minutes=60)
        compacted_result = auto_compact_messages(
            messages=conversation.messages,
            provider=provider,
            context_window=provider.context_window,
            model_max_output_tokens= call_kwargs["max_tokens"],
            state=auto_compact_state,
            **{"tool_schemas": tool_schemas, "system": system_prompt}
        )
        
        if compacted_result.error is not None:
            return finish_run(
                text="",
                subtype="error_compaction",
                errors=[
                    f"Conversation compaction failed: {compacted_result.error}"
                ],
                on_event=on_event,
                turn_count=turn_count,
                total_usage=total_usage,
            )
        conversation.messages = compacted_result.messages
        
        if tool_context.task_store is not None and compacted_result.was_compacted:
            try:
                has_tasks = bool(tool_context.task_store.list_tasks())
            except Exception as exc:
                has_tasks = False
                if tool_context.session_log is not None:
                    tool_context.session_log.record_exception(
                        "task_reminder_read_failed", exc
                    )
            if has_tasks:
                conversation.add_user_message(
                    "The conversation was compacted. Task records are stored separately. "
                    "When continuing task-related work, call TaskList to retrieve the "
                    "current task state. Use TaskGet when you need a task's full description."
                )
             
        api_messages = conversation.get_messages()
        if on_checkpoint is not None and compacted_result.was_compacted:
            on_checkpoint()

        response, streamed_live_text = _call_provider_for_turn(
            provider=provider,
            api_messages=api_messages,
            call_kwargs=call_kwargs,
            stream=stream,
            on_text_chunk=on_text_chunk,
        )
        turn_count += 1
        detector.begin_turn(response.content or "")
        # Collect usage info
        if response.usage:
            total_usage["input_tokens"] += response.usage.get("input_tokens", 0)
            total_usage["output_tokens"] += response.usage.get("output_tokens", 0)
            
        # Build assistant content
        final_assistant_content = response.content or ""
        tool_uses = response.tool_uses or []

        if response.finish_reason == "max_tokens":
            if final_assistant_content:
                conversation.add_assistant_message(
                    final_assistant_content
                )

            return finish_run(
                text=final_assistant_content,
                subtype="error_max_tokens",
                errors=[
                    "The model response reached the output-token limit "
                    "and may be incomplete."
                ],
                on_event=on_event,
                turn_count=turn_count,
                total_usage=total_usage,
            )
            
        assistant_blocks: list = []

        if response.content:
            assistant_blocks.append(TextContentBlock(type="text", text=response.content))
        
        for tool_use in tool_uses:
            assistant_blocks.append(
                ToolUseContentBlock(
                    type="tool_use",
                    id=tool_use["id"],
                    name=tool_use["name"],
                    input=tool_use["input"]
                ))
        conversation.add_assistant_message(assistant_blocks if assistant_blocks else "")
        if on_checkpoint is not None:
            on_checkpoint()
            
        # tool_uses = response.tool_uses or []
        if not tool_uses:
            if (
                stream
                and final_assistant_content
                and not streamed_live_text
            ):
                _emit_text_chunks(on_text_chunk, final_assistant_content)

            if final_assistant_content.strip():
                return finish_run(text=final_assistant_content, on_event=on_event, turn_count=turn_count, total_usage=total_usage)

            if last_user_visible_message is not None:
                if stream:
                    _emit_text_chunks(on_text_chunk, last_user_visible_message)
                return finish_run(text=last_user_visible_message, on_event=on_event, turn_count=turn_count, total_usage=total_usage)

            return finish_run(
                text="",
                subtype="error_during_execution",
                errors=[
                    "The model returned no text or tool calls "
                    f"(finish_reason={response.finish_reason!r})."
                ],
                on_event=on_event,
                turn_count=turn_count,
                total_usage=total_usage
            )
        
        # Call each tool
        for tool_index, tool_use in enumerate(tool_uses):
            tool_name = tool_use["name"]
            tool_id = tool_use["id"]
            tool_input = tool_use["input"]
            
            try:
                _safe_call_handler(
                    on_event, 
                    ToolEvent(
                        kind="tool_use",
                        tool_name=tool_name,
                        tool_input=tool_input,
                        tool_use_id=tool_id,
                    )
                )
                
                tool_call = ToolCall(
                    name=tool_name, 
                    input=tool_input, 
                    tool_use_id=tool_id
                )
                
                result = tool_registry.dispatch(tool_call, tool_context)
                result_output = result.output
                        
                _safe_call_handler(
                    on_event,
                    ToolEvent(
                        kind="tool_result",
                        tool_name=tool_name,
                        tool_output=result_output,
                        tool_use_id=tool_id,
                        is_error=result.is_error,
                    )
                )
                
                processed_result = _process_tool_result(result, tool_id, tool_context)
                conversation.add_tool_result_message(
                    tool_use_id=tool_id,
                    content=processed_result.output,
                    is_error=processed_result.is_error,
                )
                detector.record_result(tool_name, tool_input, result_output, result.is_error)
                if on_checkpoint is not None:
                    on_checkpoint()
                            
            except (KeyboardInterrupt, EOFError):
                conversation.repair_pending_tool_results()
                raise

            except Exception as e:
                error_str = f"Error: {e}"

                _safe_call_handler(
                    on_event,
                    ToolEvent(
                        kind="tool_error",
                        tool_name=tool_name,
                        tool_input=tool_input,
                        tool_use_id=tool_id,
                        is_error=True,
                        error=error_str,
                    ),
                )

                conversation.add_tool_result_message(
                    tool_id,
                    error_str,
                    is_error=True,
                )
                detector.record_result(tool_name, tool_input, error_str, True)
                if on_checkpoint is not None:
                    on_checkpoint()

        reason, check_usage, periodic_due = detector.check(provider, tool_context.session_log)
        for key in total_usage:
            total_usage[key] += check_usage.get(key, 0)
        if reason:
            reminder = detector.confirm(reason)
            if reminder is None:
                return finish_run(
                    text="", subtype="stuck",
                    errors=["Stopped after repeated unproductive actions despite runtime feedback. " + reason],
                    on_event=on_event, turn_count=turn_count, total_usage=total_usage,
                )
            conversation.add_user_message(reminder)
            if tool_context.session_log is not None:
                tool_context.session_log.record("loop_feedback", {"turn": detector.turn_count, "reason": reason})
        if periodic_due:
            try:
                task_context = format_task_context(tool_context.task_store)
            except Exception as exc:
                task_context = (
                    "\n\nTask state could not be read for this reminder. "
                    "Do not infer that the plan is empty or complete. "
                    "Use TaskList to retrieve the current state."
                )
                if tool_context.session_log is not None:
                    tool_context.session_log.record_exception(
                        "task_reminder_read_failed", exc
                    )
            conversation.add_user_message(TUTORIAL_REMINDER + task_context)
        if on_checkpoint is not None:
            on_checkpoint()

    # Reached max turns
    return finish_run(
        text="",
        subtype="error_max_turns",
        errors=[
            f"Reached maximum number of turns ({max_turns}). "
            "The task may be unfinished."
        ],
        on_event=on_event,
        turn_count=turn_count,
        total_usage=total_usage
    )
