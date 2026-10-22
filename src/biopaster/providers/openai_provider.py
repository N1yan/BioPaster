import asyncio
import json
from collections.abc import AsyncIterable, Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from .base import ChatResponse, MessageInput, prepare_messages


def convert_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Convert internal tool definitions to Chat Completions tools."""
    result = []

    for tool in tools or []:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": deepcopy(tool["input_schema"]),
            },
        })

    return result

def convert_content_blocks(
    content: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert text and image content to Chat Completions content parts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if not isinstance(content, list):
        raise ValueError("Content must be a string or a list of blocks")

    result = []

    for block in content:
        block_type = block["type"]

        if block_type == "text":
            result.append({
                "type": "text",
                "text": block["text"],
            })

        elif block_type == "image":
            source = block["source"]
            source_type = source["type"]

            if source_type == "base64":
                url = (
                    f"data:{source['media_type']};base64,"
                    f"{source['data']}"
                )
            elif source_type == "url":
                url = source["url"]
            else:
                raise ValueError(
                    f"Unsupported image source type: {source_type}"
                )

            result.append({
                "type": "image_url",
                "image_url": {"url": url},
            })

        else:
            raise ValueError(
                f"Unsupported content block type: {block_type}"
            )

    return result


def convert_assistant_message(
    content: str | list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert an assistant message, preserving tool call IDs."""
    if isinstance(content, str):
        return {"role": "assistant", "content": content}

    if not isinstance(content, list):
        raise ValueError("Assistant content must be a string or a list")

    text_parts = []
    tool_calls = []

    for block in content:
        block_type = block["type"]

        if block_type == "text":
            text_parts.append(block["text"])

        elif block_type == "tool_use":
            arguments = block["input"]
            if not isinstance(arguments, dict):
                raise ValueError("Tool call input must be an object")

            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(
                        arguments,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                },
            })

        else:
            raise ValueError(
                f"Unsupported assistant content block: {block_type}"
            )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    return message

def convert_tool_result(
    block: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a tool result, separating images without duplicating text."""
    tool_call_id = block["tool_use_id"]
    parts = convert_content_blocks(block["content"])

    text_parts = []
    visual_content = []
    image_index = 0

    for part in parts:
        if part["type"] == "text":
            text_parts.append(part["text"])
        else:
            image_index += 1
            image_id = f"{tool_call_id}_image_{image_index}"

            text_parts.append(f"[Image: {image_id}]")
            visual_content.extend([
                {
                    "type": "text",
                    "text": (
                        f"[Image {image_id} from tool call "
                        f"{tool_call_id}]"
                    ),
                },
                part,
            ])

    text = "\n".join(text_parts)
    if block.get("is_error", False):
        text = "[Tool execution failed]\n" + text

    message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": text,
    }

    return message, visual_content


def convert_messages(
    messages: list[MessageInput],
    system: str = "",
) -> list[dict[str, Any]]:
    """Convert wire formats without validating or repairing conversation history."""
    result: list[dict[str, Any]] = []
    tool_images: list[dict[str, Any]] = []

    def flush_tool_images() -> None:
        if tool_images:
            result.append({"role": "user", "content": list(tool_images)})
            tool_images.clear()

    if system:
        result.append({"role": "system", "content": system})

    for message in prepare_messages(messages):
        role = message["role"]
        content = message["content"]

        if role == "user" and isinstance(content, list):
            user_parts: list[dict[str, Any]] = []
            for block in content:
                if block["type"] == "tool_result":
                    if user_parts:
                        result.append({"role": "user", "content": user_parts})
                        user_parts = []
                    tool_message, visuals = convert_tool_result(block)
                    result.append(tool_message)
                    tool_images.extend(visuals)
                else:
                    flush_tool_images()
                    user_parts.extend(convert_content_blocks([block]))
            if user_parts:
                result.append({"role": "user", "content": user_parts})
            continue

        flush_tool_images()
        if role == "assistant":
            result.append(convert_assistant_message(content))
        elif role == "user":
            if not isinstance(content, str):
                raise ValueError("User content must be a string or a list")
            result.append({"role": "user", "content": content})
        elif role == "system":
            parts = convert_content_blocks(content)
            if any(part["type"] != "text" for part in parts):
                raise ValueError("System messages must contain only text")
            text = "\n".join(part["text"] for part in parts)
            if len(result) == 1 and result[0]["role"] == "system":
                result[0]["content"] += "\n\n" + text
            else:
                result.append({"role": "system", "content": text})
        else:
            raise ValueError(f"Unsupported message role: {role}")

    flush_tool_images()
    return result

class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        context_window: int = 128000,
        max_output_tokens: int = 32000,
        reasoning_effort: str | None = None,
        enable_thinking: bool | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.enable_thinking = enable_thinking
        self.session_log = None

    def validate_configuration(self) -> None:
        """Check required connection fields and SDK availability."""
        for name in ("api_key", "base_url", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        self._get_client_class()
        
    def _build_request(
        self,
        messages: list[MessageInput],
        tools: list[dict[str, Any]] | None = None,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Build the common Chat Completions request body."""
        request: dict[str, Any] = {
            "model": self.model if model is None else model,
            "messages": convert_messages(messages, system=system),
            "max_completion_tokens": (
                self.max_output_tokens
                if max_tokens is None
                else max_tokens
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        converted_tools = convert_tools(tools)
        if converted_tools:
            request["tools"] = converted_tools

        return request
    
    def _apply_thinking_settings(
        self,
        request: dict[str, Any],
        thinking: dict[str, Any] | None = None,
    ) -> None:
        """Translate thinking settings into OpenAI reasoning effort."""
        same_model = request["model"] == self.model
        enabled = self.enable_thinking if same_model else None
        effort = self.reasoning_effort if same_model else None

        if thinking is not None:
            if set(thinking) != {"type"}:
                raise ValueError(
                    "OpenAI thinking overrides support only 'type'; "
                    "token budgets cannot be translated to reasoning effort."
                )

            thinking_type = thinking["type"]
            if thinking_type not in ("enabled", "disabled"):
                raise ValueError(
                    "thinking.type must be 'enabled' or 'disabled'"
                )

            enabled = thinking_type == "enabled"

        if enabled is False:
            request["reasoning_effort"] = "none"
        elif effort is not None:
            request["reasoning_effort"] = effort
        elif enabled is True:
            request["reasoning_effort"] = "medium"

    @staticmethod
    def _get_client_class():
        """Load the SDK when this provider needs it."""
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ImportError(
                "The required openai dependency is missing. "
                "Run 'python -m pip install .' from the BioPaster project directory "
                "in the environment used to run BioPaster."
            ) from exc

        return AsyncOpenAI
    
    def _build_chat_response(
        self,
        *,
        content: str,
        model: str,
        usage: dict[str, Any],
        finish_reason: str | None,
        tool_calls: list[dict[str, Any]],
        reasoning_content: str = "",
    ) -> ChatResponse:
        """Normalize a fully assembled streaming response."""
        finish_reasons = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }

        if finish_reason not in finish_reasons:
            raise ValueError(
                f"Incomplete or unsupported response termination: "
                f"{finish_reason!r}"
            )

        tool_uses = []

        if finish_reason == "tool_calls":
            if not tool_calls:
                raise ValueError("Response ended with tool_calls but has none")

            seen_ids = set()

            for call in tool_calls:
                call_id = call["id"]
                function = call["function"]
                name = function["name"]

                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("Tool call ID must be a non-empty string")
                if call_id in seen_ids:
                    raise ValueError(f"Duplicate tool call ID: {call_id}")
                if not isinstance(name, str) or not name:
                    raise ValueError("Tool name must be a non-empty string")

                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")

                seen_ids.add(call_id)
                tool_uses.append({
                    "id": call_id,
                    "name": name,
                    "input": arguments,
                })

        elif finish_reason == "stop" and tool_calls:
            raise ValueError(
                "Response contains tool calls but ended with stop"
            )

        return ChatResponse(
            content=content,
            model=model,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            finish_reason=finish_reasons[finish_reason],
            reasoning_content=reasoning_content or None,
            tool_uses=tool_uses or None,
        )
        
        
    async def _consume_stream(
        self,
        stream: AsyncIterable[Any],
        *,
        model: str,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """Assemble streaming chunks before exposing executable tool calls."""
        text_parts = []
        reasoning_parts = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason = None
        response_model = model

        async for chunk in stream:
            if chunk.model:
                response_model = chunk.model

            if chunk.usage is not None:
                usage = chunk.usage.model_dump()

            # The final usage chunk may contain no choices.
            if not chunk.choices:
                continue

            if len(chunk.choices) != 1 or chunk.choices[0].index != 0:
                raise ValueError("Expected a single response choice")

            choice = chunk.choices[0]
            delta = choice.delta

            if finish_reason is not None:
                raise ValueError("Received choice data after response termination")

            if delta.refusal:
                raise RuntimeError(f"Model refused the request: {delta.refusal}")

            if delta.function_call is not None:
                raise ValueError("Legacy function_call responses are unsupported")

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)

            if delta.content:
                text_parts.append(delta.content)
                if on_text_chunk is not None:
                    on_text_chunk(delta.content)

            for fragment in delta.tool_calls or []:
                if fragment.type not in (None, "function"):
                    raise ValueError(
                        f"Unsupported tool call type: {fragment.type}"
                    )

                call = tool_calls.setdefault(
                    fragment.index,
                    {
                        "id": "",
                        "function": {"name": "", "arguments": ""},
                    },
                )

                if fragment.id:
                    call["id"] += fragment.id

                if fragment.function is not None:
                    if fragment.function.name:
                        call["function"]["name"] += fragment.function.name
                    if fragment.function.arguments:
                        call["function"]["arguments"] += (
                            fragment.function.arguments
                        )

            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason

        return self._build_chat_response(
            content="".join(text_parts),
            model=response_model,
            usage=usage,
            finish_reason=finish_reason,
            tool_calls=[
                tool_calls[index] for index in sorted(tool_calls)
            ],
            reasoning_content="".join(reasoning_parts),
        )
    
    async def _stream_response(
        self,
        client: Any,
        request: dict[str, Any],
        *,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """Open and consume one response stream."""
        stream = await client.chat.completions.create(**request)

        async with stream:
            return await self._consume_stream(
                stream,
                model=request["model"],
                on_text_chunk=on_text_chunk,
            )
              
        
    async def _send_request(
        self,
        request: dict[str, Any],
        *,
        on_text_chunk: Callable[[str], None] | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> ChatResponse:
        """Send one streaming request and close its resources on exit."""
        client_class = self._get_client_class()

        client_options: dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

        if max_retries is not None:
            client_options["max_retries"] = max_retries

        if timeout is not None:
            client_options["timeout"] = timeout

        # Unlike the SDK's per-read timeout, this also bounds an active stream.
        async with asyncio.timeout(total_timeout):
            async with client_class(**client_options) as client:
                return await self._stream_response(
                    client,
                    request,
                    on_text_chunk=on_text_chunk,
                )
              
    def chat_stream_response(
        self,
        messages: list[MessageInput],
        tools: list[dict[str, Any]] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        max_retries: int | None = None,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict[str, Any] | None = None,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> ChatResponse:
        """Provide the synchronous interface used by the agent."""
        self.validate_configuration()

        # Avoid creating a coroutine that asyncio.run cannot execute.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "chat_stream_response must run outside an active event loop"
            )

        request_id = uuid4().hex
        log = self.session_log

        try:
            request = self._build_request(
                messages,
                tools,
                system=system,
                model=model,
                max_tokens=max_tokens,
            )
            self._apply_thinking_settings(request, thinking)

            if log is not None:
                log.record(
                    "model_request",
                    {"request_id": request_id, "request": request},
                )

            response = asyncio.run(
                self._send_request(
                    request,
                    on_text_chunk=on_text_chunk,
                    max_retries=max_retries,
                    timeout=timeout,
                    total_timeout=total_timeout,
                )
            )

            if log is not None:
                log.record(
                    "model_response",
                    {"request_id": request_id, "response": response},
                )

            return response

        except (Exception, KeyboardInterrupt) as exc:
            if log is not None:
                log.record(
                    "model_request_failed",
                    {
                        "request_id": request_id,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                log.record_exception("model_exception", exc)
            raise
