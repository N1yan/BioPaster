"""Context-budget tests for automatic conversation compaction."""

import pytest

from biopaster.agent.conversation import Message
from biopaster.compact.auto_compact import (
    AutoCompactState,
    _get_compact_threshold,
    auto_compact_messages,
)
from biopaster.providers.base import ChatResponse


class SummaryProvider:
    def __init__(self):
        self.requested_max_tokens = None

    def chat_stream_response(self, messages, **kwargs):
        self.requested_max_tokens = kwargs["max_tokens"]
        return ChatResponse(
            content="<summary>Preserved task state.</summary>",
            model="fake",
            usage={"output_tokens": 8},
            finish_reason="end_turn",
        )


@pytest.mark.parametrize(("context_window", "expected_threshold"), [
    (16_384, 6_554),
    (32_768, 21_300),
    (128_000, 107_008),
])
def test_compact_threshold_adapts_to_context_window(context_window, expected_threshold):
    assert _get_compact_threshold(context_window, 8192) == expected_threshold


def test_compact_rejects_output_budget_that_consumes_context_window():
    with pytest.raises(ValueError, match="too small"):
        _get_compact_threshold(8192, 8192)


def test_small_context_caps_summary_output_to_one_quarter():
    provider = SummaryProvider()
    state = AutoCompactState()
    messages = [Message(role="user", content="x" * 22_000)]

    result = auto_compact_messages(
        messages=messages,
        provider=provider,
        context_window=16_384,
        model_max_output_tokens=8192,
        state=state,
        system="",
        tool_schemas=[],
    )

    assert result.was_compacted is True
    assert result.summary_tokens == 8
    assert provider.requested_max_tokens == 4096
    assert result.messages[0].content.endswith("Preserved task state.")
