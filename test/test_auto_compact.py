"""Compaction retries must be bounded and must not mutate the original history."""

from copy import deepcopy
import importlib

import httpx
import pytest

from biopaster.agent.conversation import Conversation
from biopaster.compact.auto_compact import (
    AutoCompactState,
    COMPACT_PROMPT,
    auto_compact_messages,
)
from biopaster.providers.base import ChatResponse
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.registry import ToolRegistry

compact = importlib.import_module("biopaster.compact.auto_compact")
loop = importlib.import_module("biopaster.agent.agent_loop")


class StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Provider:
    context_window = 4096
    max_output_tokens = 1024

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []

    def chat_stream_response(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        assert kwargs["max_retries"] == 0
        if self.errors:
            raise self.errors.pop(0)
        return ChatResponse("<summary>Summary.</summary>", "test", {}, "end_turn")


@pytest.fixture
def conversation():
    result = Conversation()
    result.add_user_message("x" * 9000)
    return result


@pytest.fixture
def waits(monkeypatch):
    result = []
    monkeypatch.setattr(compact.time, "sleep", result.append)
    return result


def run_compact(conversation, provider, state):
    return auto_compact_messages(
        conversation.messages,
        provider,
        context_window=provider.context_window,
        model_max_output_tokens=provider.max_output_tokens,
        state=state,
    )


def test_transient_failures_retry_without_duplicating_summary_prompt(conversation, waits):
    before = conversation.to_dict()
    provider = Provider([TimeoutError("timeout"), httpx.ReadError("connection lost")])
    state = AutoCompactState()
    result = run_compact(conversation, provider, state)
    assert result.was_compacted and result.error is None
    assert len(provider.calls) == 3
    assert waits == [1, 2]
    assert state.consecutive_failures == 0
    assert conversation.to_dict() == before
    assert all(call == provider.calls[0] for call in provider.calls)
    for call in provider.calls:
        assert sum(message.get("content") == COMPACT_PROMPT for message in call) == 1


def test_failed_compaction_stops_without_sending_normal_request(conversation, waits, monkeypatch, tmp_path):
    before = conversation.get_messages()
    provider = Provider([TimeoutError("timeout")] * 3)
    events, checkpoints = [], []
    monkeypatch.setattr(loop, "assemble_system_prompt", lambda _: "Test")
    result = loop.agent_loop(
        conversation, provider, ToolRegistry(), ToolContext(workspace_root=tmp_path),
        on_event=events.append, on_checkpoint=lambda: checkpoints.append(True),
    )
    assert len(provider.calls) == 3
    assert waits == [1, 2]
    assert result.subtype == "error_compaction" and result.num_turns == 0
    assert "TimeoutError" in result.errors[0]
    assert checkpoints and events[-1].is_error
    assert conversation.get_messages() == before


@pytest.mark.parametrize("status, retry", [(400, False), (401, False), (429, True), (503, True)])
def test_only_temporary_http_errors_retry(conversation, waits, status, retry):
    provider = Provider([StatusError(status)])
    result = run_compact(conversation, provider, AutoCompactState())
    assert result.was_compacted is retry
    assert len(provider.calls) == (2 if retry else 1)
    assert waits == ([1] if retry else [])
    if not retry:
        assert result.error and result.messages is conversation.messages


@pytest.mark.parametrize("mixed", [False, True])
def test_grouped_errors_retry_only_if_all_causes_are_temporary(conversation, waits, mixed):
    errors = [TimeoutError("timeout")]
    if mixed:
        errors.append(StatusError(400))
    provider = Provider([ExceptionGroup("group", errors)])
    result = run_compact(conversation, provider, AutoCompactState())
    assert result.was_compacted is (not mixed)
    assert len(provider.calls) == (1 if mixed else 2)


def test_user_interrupt_is_not_retried(conversation, waits):
    provider = Provider([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        run_compact(conversation, provider, AutoCompactState())
    assert len(provider.calls) == 1 and not waits
