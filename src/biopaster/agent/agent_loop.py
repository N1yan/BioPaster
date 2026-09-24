
import json
from typing import Any, Callable
from ..providers.base import ChatResponse
from dataclasses import dataclass
from ..tool_system.registry import ToolRegistry
from .conversation import Conversation, TextContentBlock, ToolUseContentBlock
from ..tool_system.context import ToolContext
from .prompts import assemble_system_prompt
from ..tool_system.protocol import ToolResult, ToolCall
from ..tool_system.persist import Persist
from pathlib import Path
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
TOOL_RESULT_DIR = Path.home() / ".biopaster/.tool_results"
TOOL_RESULT_DIR.mkdir(exist_ok=True)
persist = Persist(TOOL_RESULT_DIR, max_content_length=20000)

def _process_tool_result(tool_result: ToolResult, tool_use_id):
    tool_result = persist.persist(tool_result, tool_use_id)
    return map_tool_result(tool_result)
    # if "gemma" in MODEL:
    #     return map_tool_result_gemma(tool_result)
    # else:
    #     return map_tool_result(tool_result)
    
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
        usage=total_usage if any(total_usage.values()) else None,
        num_turns=turn_count,
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
    )-> AgentLoopResult:
    """Run agent loop: LLM -> tools -> LLM until no more tools or max turns.
    Returns:
        AgentLoopResult with final text response, usage info, and turn count
    """
    
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
        
        conversation.messages = compacted_result.messages
             
        api_messages = conversation.get_messages()

         
        response, streamed_live_text = _call_provider_for_turn(
            provider=provider,
            api_messages=api_messages,
            call_kwargs=call_kwargs,
            stream=stream,
            on_text_chunk=on_text_chunk,
        )
        turn_count += 1
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

            return finish(
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
        
        # tool_uses = response.tool_uses or []
        if not tool_uses:
            if (
                stream
                and final_assistant_content
                and not streamed_live_text
            ):
                _emit_text_chunks(on_text_chunk, final_assistant_content)

            if final_assistant_content.strip():
                return finish(text=final_assistant_content, on_event=on_event, turn_count=turn_count, total_usage=total_usage)

            if last_user_visible_message is not None:
                if stream:
                    _emit_text_chunks(on_text_chunk, last_user_visible_message)
                return finish(text=last_user_visible_message, on_event=on_event, turn_count=turn_count, total_usage=total_usage)

            return finish(
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
                
                processed_result = _process_tool_result(result, tool_id)
                conversation.add_tool_result_message(
                    tool_use_id=tool_id,
                    content=processed_result.output,
                    is_error=processed_result.is_error,
                )
                            
            except (KeyboardInterrupt, EOFError):
                for pending_index in range(tool_index, len(tool_uses)):
                    pending_tool = tool_uses[pending_index]

                    if pending_index == tool_index:
                        message = (
                            "Tool call interrupted. Execution may be incomplete; "
                            "verify the current state before retrying."
                        )
                    else:
                        message = (
                            "Tool call cancelled because the task was interrupted. "
                            "This tool call was not executed."
                        )

                    conversation.add_tool_result_message(
                        tool_use_id=pending_tool["id"],
                        content=message,
                        is_error=True,
                    )

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
    
    # Reached max turns
    return finish(
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
