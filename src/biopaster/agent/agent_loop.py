
import json
from typing import Any, Callable
from ..providers.base import ChatResponse
from dataclasses import dataclass
from ..tool_system.registry import ToolRegistry
from .conversation import Conversation, TextContentBlock, ToolUseContentBlock
from ..tool_system.context import ToolContext
from ..tool_system.protocol import ToolResult, ToolCall
from ..tool_system.persist import Persist
from pathlib import Path
from ..tool_system.map_result import map_tool_result

# ── System Prompt ──
PROMPT_SECTIONS = {
    "identity": ("You are BioPaster, a helpful assistant that can help researchers do biology research, "
                 "including but not limited to literature review, data analysis, and experimental design."
                 "[NOTE] Important: since we are currently in the development stage, in addition to your regular responses,"
                 "you are required to report any errors, warnings, or difficulties you encounter while performing"
                 "tasks. Whenever an error or warning occurs, you must provide the exact error/warning message verbatim"),
    "tools": "Available tools:",
    "language": "You should use the language of the user's question to respond.",
    "memory": "Relevant memories are injected below when available."
}

def assemble_system_prompt(context: ToolContext) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["language"]]
    # sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + "")
    # if context["memories"]:
    #     sections.append(f"Relevant memories:\n{context['memories']}")
    if context.workspace_root:
        sections.append(f"Working directory:\n{context.workspace_root}")
    
    if context.tools:
        tool_lines = "\n".join(
            f"- {tool_name}"
            for tool_name in context.tools
        )
        sections.append(f"Available tools:\n{tool_lines}")
    
    if context.mcp_clients:
        mcp_names = list(context.mcp_clients.keys())
        if mcp_names:
            sections.append(f"Connected MCP servers:\n{', '.join(mcp_names)}")
    return "\n\n".join(sections)

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
    
def _safe_call_handler(handler: Callable | None, event: ToolEvent):
    if handler is None:
        return
    try:
        handler(event)
    except Exception:
        return

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

# ── Agent Loop ──
def agent_loop(
    conversation: Conversation,
    provider,
    tool_registry: ToolRegistry,
    tool_context: ToolContext,
    max_turns: int = 20,
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
    
    total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    turn_count = 0
    for turn in range(max_turns):
        api_messages = conversation.get_messages()
        call_kwargs: dict[str, Any] = {"tools": tool_schemas}
        call_kwargs["system"] = system_prompt
         
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
        
        assistant_blocks: list = []
        if response.content:
            assistant_blocks.append(TextContentBlock(type="text", text=response.content))
        
        tool_uses = response.tool_uses or []
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
            if stream and final_assistant_content and not streamed_live_text:
                _emit_text_chunks(on_text_chunk, final_assistant_content)
            if (final_assistant_content or "").strip() == "" and last_user_visible_message is not None:
                return AgentLoopResult(
                    response_text=last_user_visible_message,
                    usage=total_usage if total_usage["input_tokens"] > 0 or total_usage["output_tokens"] > 0 else None,
                    num_turns=turn_count,
                )
            return AgentLoopResult(
                response_text=final_assistant_content,
                usage=total_usage if total_usage["input_tokens"] > 0 or total_usage["output_tokens"] > 0 else None,
                num_turns=turn_count,
                )
        
        # Call each tool
        for tool_use in tool_uses:
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
                if tool_name.lower() == "sendusermessage" and isinstance(result_output, dict):
                    msg = result_output.get("message")
                    if isinstance(msg, str):
                        last_user_visible_message = msg
                if tool_name.lower() == "structuredoutput" and isinstance(result_output, dict):
                    payload = result_output.get("structured_output")
                    try:
                        last_user_visible_message = json.dumps(payload, ensure_ascii=False, indent=2)
                    except Exception:
                        last_user_visible_message = str(payload)
                        
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
                conversation.add_tool_result_message(tool_id, processed_result.output)
            
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
                
                conversation.add_tool_result_message(tool_id, error_str, is_error=True)
    
    # Reached max turns
    return AgentLoopResult(
        response_text="[Max tool turns reached]",
        usage=total_usage if total_usage["input_tokens"] > 0 or total_usage["output_tokens"] > 0 else None,
        num_turns=turn_count,
    )