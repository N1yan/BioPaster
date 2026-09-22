from typing import Any, Optional, Callable
from anthropic import Anthropic
from .base import ChatResponse, prepare_messages

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
        **kwargs
    ) -> ChatResponse:
        
        model = kwargs.get("model", self.model)
        max_tokens = kwargs.get("max_tokens", 4096)
        anthropic_messages = prepare_messages(messages)
        system = kwargs.pop("system", {})
        # Make API call
        client = self._ensure_client()
        
        extra_kwargs: dict[str, Any] = {}
        if tools:
            extra_kwargs["tools"] = tools
        
        streamed_text = ""
        with client.messages.stream(
            model=model,
            messages=anthropic_messages,
            max_tokens=max_tokens,
            system=system,
            **extra_kwargs,
            **{k: v for k, v in kwargs.items() if k not in ["model", "max_tokens", "tools"]}
        ) as stream:
            for text in stream.text_stream:
                if text:
                    streamed_text += text
                if on_text_chunk:
                    on_text_chunk(text)
            try:
                final_message = stream.get_final_message()
                print({
                    "stop_reason": final_message.stop_reason,
                    "block_types": [
                        block.type for block in final_message.content
                    ],
                    "usage": final_message.usage.model_dump(),
                })
            except Exception:
                # final_message = None
                raise
                
            if final_message is not None:
                return self._build_chat_response(final_message)
        
        return ChatResponse(
            content=streamed_text,
            model=model,
            usage={},
            finish_reason="stop",
            tool_uses=None
        )

