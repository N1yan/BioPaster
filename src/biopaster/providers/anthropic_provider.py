from typing import Any, Optional, Callable
from anthropic import Anthropic
from .base import ChatResponse, prepare_messages
from uuid import uuid4

class AnthropicProvider:
    def __init__(self, 
                 api_key: str, 
                 base_url: str, 
                 model: str, 
                 context_window: int = 128000, 
                 max_output_tokens: int = 8192):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = None
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.session_log = None

    def _ensure_client(self):
        if self.client is None:
            self.client = Anthropic(api_key=self.api_key, 
                                    base_url=self.base_url)
        return self.client
    
    def _build_chat_response(self, response: Any) -> ChatResponse:
        """Convert Anthropic SDK response into the shared ChatResponse shape."""
        content_text = ""
        tool_uses: list[dict[str, Any]] = []
        
        for block in response.content:
            block_type = getattr(block, "type", "text")
            if block_type == "text":
                block_text = getattr(block, "text", "")
                content_text += block_text
            elif block_type == "tool_use":
                tool_uses.append({
                    "id": str(getattr(block, "id", "")),
                    "name": str(getattr(block, "name", "")),
                    "input": dict(getattr(block, "input", {})),
                })
        usage = getattr(response, "usage", None)
        return ChatResponse(
            content=content_text,
            model=getattr(response, "model", self.model or ""),
            usage={
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            },
            finish_reason=str(getattr(response, "stop_reason", "stop")),
            tool_uses=tool_uses if tool_uses else None,
        )

    def chat_stream_response(
        self,
        messages: list,
        tools: Optional[list[dict[str, Any]]] = None,
        on_text_chunk: Callable[[str], None] | None = None,
        **kwargs,
    ) -> ChatResponse:
        request = {
            **kwargs,
            "model": kwargs.get("model", self.model),
            "messages": prepare_messages(messages),
            "max_tokens": kwargs.get("max_tokens", 4096),
            "system": kwargs.get("system", {}),
        }

        if tools:
            request["tools"] = tools

        request_id = uuid4().hex
        log = self.session_log

        if log is not None:
            log.record(
                "model_request",
                {
                    "request_id": request_id,
                    "request": request,
                },
            )

        try:
            client = self._ensure_client()

            with client.messages.stream(**request) as stream:
                for event in stream:
                    if log is not None:
                        log.record(
                            "model_stream",
                            {
                                "request_id": request_id,
                                "event": event,
                            },
                        )

                    if event.type == "text" and on_text_chunk is not None:
                        on_text_chunk(event.text)

                final_message = stream.get_final_message()

            if log is not None:
                log.record(
                    "model_response",
                    {
                        "request_id": request_id,
                        "response": final_message,
                    },
                )

            return self._build_chat_response(final_message)

        except (Exception, KeyboardInterrupt) as e:
            if log is not None:
                log.record(
                    "model_request_failed",
                    {
                        "request_id": request_id,
                        "exception_type": type(e).__name__,
                        "message": str(e),
                    },
                )
                log.record_exception("model_exception", e)
            raise
