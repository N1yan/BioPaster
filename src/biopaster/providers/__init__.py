
from .base import ChatMessage, ChatResponse

def get_provider_class(provider_name: str):
    """Get provider class by name."""
    if provider_name in ["openrouter", "anthropic", "qwen"]:
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider

__all__= [
    "ChatMessage",
    "ChatResponse",
    "get_provider_class",
]