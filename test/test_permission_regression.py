"""Offline regression tests for permission enforcement and interruption recovery."""

import importlib
from types import SimpleNamespace

import pytest

from biopaster.agent.conversation import Conversation
from biopaster.providers.base import ChatResponse
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.permissions import (
    PermissionAnswer,
    PermissionRequest,
    PermissionRule,
    PermissionTarget,
)
from biopaster.tool_system.persist import Persist
from biopaster.tool_system.protocol import ToolCall, ToolResult
from biopaster.tool_system.registry import ToolRegistry, ToolSpec


class CounterTool:
    """An observable tool with no shell, network, or file side effects."""

    def __init__(self):
        self.executions = 0

    def spec(self):
        return ToolSpec("Counter", "Increment a counter.", {"type": "object"})

    def prepare_input(self, tool_input, context):
        return tool_input

    def check_permissions(self, tool_input, context):
        return PermissionRequest(
            tool_name="Counter",
            tool_use_id=None,
            description="Increment a counter.",
            targets=(PermissionTarget("Counter", "increment"),),
            suggestions=(PermissionRule("Counter", "increment"), PermissionRule("Counter")),
        )

    def run(self, tool_input, context):
        self.executions += 1
        return ToolResult("Counter", [{"type": "text", "content": "Completed."}])


@pytest.fixture
def setup_tool(tmp_path):
    tool = CounterTool()
    return tool, ToolRegistry([tool]), ToolContext(workspace_root=tmp_path)


@pytest.mark.parametrize("rule_denial", [False, True])
def test_denial_prevents_execution(setup_tool, rule_denial):
    tool, registry, context = setup_tool
    confirmations = []

    def deny(request):
        confirmations.append(request)
        return PermissionAnswer(False)

    context.permission_context.permission_handler = deny
    if rule_denial:
        context.permission_context.add_rule(PermissionRule("Counter", behavior="deny"))

    result = registry.dispatch(ToolCall("Counter", {}, "denied-call"), context)

    assert result.is_error
    assert result.tool_use_id == "denied-call"
    assert tool.executions == 0
    assert len(confirmations) == (0 if rule_denial else 1)


def test_session_approval_and_revocation(setup_tool):
    tool, registry, context = setup_tool
    confirmations = []

    def confirm(request):
        confirmations.append(request)
        return PermissionAnswer(True, (request.suggestions[0],))

    permissions = context.permission_context
    permissions.permission_handler = confirm
    call = ToolCall("Counter", {}, "approved-call")
    assert not registry.dispatch(call, context).is_error
    assert not registry.dispatch(call, context).is_error
    assert tool.executions == 2
    assert len(confirmations) == 1
    assert len(permissions.session_rules) == 1

    permissions.remove_rule(permissions.session_rules[0])
    assert not registry.dispatch(call, context).is_error
    assert tool.executions == 3
    assert len(confirmations) == 2


def test_partial_rule_save_failure_rolls_back(setup_tool, monkeypatch):
    tool, registry, context = setup_tool
    permissions = context.permission_context
    permissions.add_rule(PermissionRule("Existing", "keep"))
    previous_rules = permissions.session_rules.copy()
    permissions.permission_handler = lambda request: PermissionAnswer(True, request.suggestions)
    original_add = permissions.add_rule
    attempts = []

    def fail_second_rule(rule):
        attempts.append(rule)
        if len(attempts) == 2:
            raise RuntimeError("Simulated rule storage failure")
        original_add(rule)

    monkeypatch.setattr(permissions, "add_rule", fail_second_rule)
    result = registry.dispatch(ToolCall("Counter", {}, "rollback-call"), context)

    assert result.is_error
    assert len(attempts) == 2
    assert tool.executions == 0
    assert permissions.session_rules == previous_rules


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
@pytest.mark.parametrize("interrupt_at", [1, 2, 3])
def test_interruption_preserves_results_and_allows_next_turn(
    setup_tool, tmp_path, monkeypatch, interrupt, interrupt_at
):
    loop = importlib.import_module("biopaster.agent.agent_loop")
    tool, registry, context = setup_tool
    conversation = Conversation()
    conversation.add_user_message("Run three counters.")
    confirmations = []

    def confirm(request):
        confirmations.append(request)
        if len(confirmations) == interrupt_at:
            raise interrupt("Simulated confirmation interruption")
        return PermissionAnswer(True)

    context.permission_context.permission_handler = confirm
    # Keep the real loop and result conversion; isolate unrelated compaction and storage.
    monkeypatch.setattr(loop, "micro_compact_messages", lambda messages, **kwargs: messages)
    monkeypatch.setattr(
        loop, "auto_compact_messages", lambda **kwargs: SimpleNamespace(messages=kwargs["messages"])
    )
    monkeypatch.setattr(loop, "assemble_system_prompt", lambda context: "Test agent.")
    monkeypatch.setattr(loop, "persist", Persist(tmp_path / "results"))

    class FakeProvider:
        context_window = 128_000
        calls = 0

        def chat_stream_response(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    content="", model="fake", usage={}, finish_reason="tool_use",
                    tool_uses=[
                        {"id": f"call_{i}", "name": "Counter", "input": {}}
                        for i in range(3)
                    ],
                )
            results = [
                block
                for message in messages if isinstance(message["content"], list)
                for block in message["content"] if block.get("type") == "tool_result"
            ]
            assert [block["tool_use_id"] for block in results] == ["call_0", "call_1", "call_2"]
            return ChatResponse("Recovered.", "fake", {}, "end_turn")

    provider = FakeProvider()
    with pytest.raises(interrupt):
        loop.agent_loop(conversation, provider, registry, context, max_turns=2)

    assert provider.calls == 1
    assert tool.executions == interrupt_at - 1
    assert len(confirmations) == interrupt_at
    assert context.active_tool_use_id is None
    results = [
        block
        for message in conversation.get_messages() if isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    ]
    assert [block["tool_use_id"] for block in results] == ["call_0", "call_1", "call_2"]
    assert [block["is_error"] for block in results] == (
        [False] * (interrupt_at - 1) + [True] * (4 - interrupt_at)
    )
    for block in results[:interrupt_at - 1]:
        assert "Completed." in str(block["content"])

    conversation.add_user_message("Continue chatting.")
    result = loop.agent_loop(conversation, provider, registry, context, max_turns=1)
    assert result.response_text == "Recovered."
    assert provider.calls == 2
    assert tool.executions == interrupt_at - 1
