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
                 max_output_tokens: int = 32000,
                 reasoning_effort: str | None = None,
                 enable_thinking: bool | None = None,
                 llama_cpp: bool = False):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = None
        self.context_window = context_window
        self.reasoning_effort = reasoning_effort
        self.enable_thinking = enable_thinking
        self.llama_cpp = llama_cpp
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else 32000
        )
        self.session_log = None

    def _ensure_client(self):
        if self.client is None:
            self.client = Anthropic(api_key=self.api_key, 
                                    base_url=self.base_url)
        return self.client
    
    def validate_configuration(self) -> None:
        """Validate configuration using the existing client initialization."""
        self._ensure_client()
    
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

    def _apply_thinking_settings(self, request: dict) -> None:
        """Resolve per-call overrides before translating the endpoint's wire format."""
        extra = dict(request.get("extra_body") or {})
        thinking = extra.pop("thinking", request.pop("thinking", None))
        output = {**(request.pop("output_config", None) or {}), **(extra.pop("output_config", None) or {})}
        same_model = request["model"] == self.model
        if thinking is None and same_model and self.enable_thinking is not None:
            thinking = {"type": "enabled" if self.enable_thinking else "disabled"}
        thinking = dict(thinking) if thinking is not None else None
        disabled = thinking is not None and thinking.get("type") == "disabled"
        if disabled:
            output.pop("effort", None)
        elif (same_model and self.reasoning_effort is not None and not self.llama_cpp
              and (thinking is None or "budget_tokens" not in thinking)):
            output.setdefault("effort", self.reasoning_effort)
        if self.llama_cpp:
            template = dict(extra.get("chat_template_kwargs") or {})
            if disabled:
                template.pop("reasoning_effort", None)
            elif (same_model and self.reasoning_effort is not None
                  and (thinking is None or "budget_tokens" not in thinking)):
                template.setdefault("reasoning_effort", self.reasoning_effort)
            if template:
                extra["chat_template_kwargs"] = template
            else:
                extra.pop("chat_template_kwargs", None)
        if self.llama_cpp and thinking is not None:
            if thinking.get("type") in ("enabled", "disabled"):
                extra["chat_template_kwargs"] = {
                    **(extra.get("chat_template_kwargs") or {}),
                    "enable_thinking": not disabled,
                }
            # llama.cpp's Messages adapter ignores disabled and otherwise defaults
            # enabled-without-budget to 10000. Keep the budget compatibility mapping
            # alongside the native template toggle (budget 0 alone is insufficient).
            if disabled:
                thinking = {"type": "enabled", "budget_tokens": 0}
            elif thinking.get("type") == "enabled":
                thinking.setdefault("budget_tokens", -1)
        if thinking is not None:
            request["thinking"] = thinking
        if output:
            extra["output_config"] = output
        if extra:
            request["extra_body"] = extra
        else:
            request.pop("extra_body", None)

    def chat_stream_response(
        self,
        messages: list,
        tools: Optional[list[dict[str, Any]]] = None,
        on_text_chunk: Callable[[str], None] | None = None,
        max_retries: int | None = None,
        **kwargs,
    ) -> ChatResponse:
        request = {
            **kwargs,
            "model": kwargs.get("model", self.model),
            "messages": prepare_messages(messages),
            "max_tokens": kwargs.get("max_tokens") or self.max_output_tokens,
            "system": kwargs.get("system", {}),
        }

        if tools:
            request["tools"] = tools

        self._apply_thinking_settings(request)

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
            if max_retries is not None:
                client = client.with_options(max_retries=max_retries)

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
