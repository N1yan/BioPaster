from typing import Any
import asyncio
import math
from collections.abc import Callable
from .openai_provider import OpenAIProvider
from .base import ChatResponse


class LlamaCppProvider(OpenAIProvider):
    def __init__(
        self,
        *args: Any,
        thinking_timeout_seconds: float | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)

        if thinking_timeout_seconds is not None:
            if (
                type(thinking_timeout_seconds) not in (int, float)
                or thinking_timeout_seconds <= 0
                or not math.isfinite(thinking_timeout_seconds)
            ):
                raise ValueError(
                    "thinking_timeout_seconds must be a positive finite number"
                )

        self.thinking_timeout_seconds = thinking_timeout_seconds
        
    def _apply_thinking_settings(
        self,
        request: dict[str, Any],
        thinking: dict[str, Any] | None = None,
    ) -> None:
        """Translate thinking settings into llama.cpp template parameters."""
        same_model = request["model"] == self.model
        enabled = self.enable_thinking if same_model else None
        effort = self.reasoning_effort if same_model else None

        if thinking is not None:
            if set(thinking) != {"type"}:
                raise ValueError("Thinking overrides support only 'type'")

            thinking_type = thinking["type"]
            if thinking_type not in ("enabled", "disabled"):
                raise ValueError(
                    "thinking.type must be 'enabled' or 'disabled'"
                )

            enabled = thinking_type == "enabled"

        template = {}

        if enabled is not None:
            template["enable_thinking"] = enabled

        if enabled is not False and effort is not None:
            template["reasoning_effort"] = effort

        if template:
            extra = request.setdefault("extra_body", {})
            extra["chat_template_kwargs"] = {
                **extra.get("chat_template_kwargs", {}),
                **template,
            }
            
    async def _end_reasoning(
        self,
        client: Any,
        *,
        completion_id: str,
        model: str,
    ) -> dict[str, Any]:
        """Request reasoning termination for one active completion."""
        if not completion_id:
            raise ValueError("A completion ID is required for reasoning control")

        response = await client.with_options(
            timeout=10.0,
            max_retries=0,
        ).post(
            "/chat/completions/control",
            cast_to=dict,
            body={
                "id": completion_id,
                "action": "reasoning_end",
                "model": model,
            },
        )

        if (
            not isinstance(response, dict)
            or type(response.get("success")) is not bool
        ):
            raise RuntimeError("Invalid reasoning control response")

        return response
    
    async def _stream_response(
        self,
        client: Any,
        request: dict[str, Any],
        *,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """Receive a response while independently limiting reasoning time."""
        timeout = self.thinking_timeout_seconds
        extra = request.get("extra_body") or {}
        template = extra.get("chat_template_kwargs") or {}

        if timeout is None or template.get("enable_thinking") is False:
            return await super()._stream_response(
                client,
                request,
                on_text_chunk=on_text_chunk,
            )

        controlled_request = {
            **request,
            "extra_body": {
                **extra,
                "reasoning_control": True,
            },
        }

        reasoning_started = asyncio.Event()
        reasoning_finished = asyncio.Event()
        completion_id = None
        deadline = None
        loop = asyncio.get_running_loop()

        async def observe_stream(stream):
            nonlocal completion_id, deadline

            async for chunk in stream:
                if chunk.id:
                    completion_id = chunk.id

                for choice in chunk.choices:
                    delta = choice.delta

                    if (
                        delta.content
                        or delta.tool_calls
                        or delta.refusal
                        or choice.finish_reason is not None
                    ):
                        reasoning_finished.set()

                    if (
                        getattr(delta, "reasoning_content", None)
                        and deadline is None
                        and not reasoning_finished.is_set()
                    ):
                        deadline = loop.time() + timeout
                        reasoning_started.set()

                yield chunk

        async def watch_reasoning():
            await reasoning_started.wait()

            remaining = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(
                    reasoning_finished.wait(),
                    timeout=remaining,
                )
                return
            except TimeoutError:
                pass

            if reasoning_finished.is_set():
                return

            if not completion_id:
                raise RuntimeError(
                    "Cannot end reasoning: no completion ID was received"
                )

            try:
                response = await self._end_reasoning(
                    client,
                    completion_id=completion_id,
                    model=request["model"],
                )
            except Exception:
                if reasoning_finished.is_set():
                    return
                raise

            if not response["success"] and not reasoning_finished.is_set():
                detail = response.get("message", "No reason provided")
                raise RuntimeError(
                    f"Reasoning control failed: {detail}"
                )

        stream = await client.chat.completions.create(**controlled_request)

        async with stream:
            async with asyncio.TaskGroup() as tasks:
                watchdog = tasks.create_task(watch_reasoning())

                try:
                    response = await self._consume_stream(
                        observe_stream(stream),
                        model=request["model"],
                        on_text_chunk=on_text_chunk,
                    )
                finally:
                    reasoning_finished.set()
                    watchdog.cancel()

        return response