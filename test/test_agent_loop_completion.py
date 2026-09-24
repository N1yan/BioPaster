"""Completion-state regression tests for the agent loop."""

from biopaster.agent.agent_loop import ResultEvent, agent_loop
from biopaster.agent.conversation import Conversation, ToolUseContentBlock
from biopaster.providers.base import ChatResponse
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.registry import ToolRegistry, ToolSpec


class StaticProvider:
    context_window = 128_000
    max_output_tokens = 8192

    def __init__(self, response: ChatResponse):
        self.response = response

    def chat_stream_response(self, messages, **kwargs):
        assert kwargs["max_tokens"] == self.max_output_tokens
        return self.response


class CountingTool:
    def __init__(self):
        self.executions = 0

    def spec(self):
        return ToolSpec("Counter", "Increment a counter.", {"type": "object"})

    def prepare_input(self, tool_input, context):
        return tool_input

    def check_permissions(self, tool_input, context):
        raise AssertionError("A tool from a truncated response must not be dispatched.")

    def run(self, tool_input, context):
        self.executions += 1
        raise AssertionError("A tool from a truncated response must not execute.")


def test_max_tokens_keeps_partial_text_and_reports_error(tmp_path):
    conversation = Conversation()
    conversation.add_user_message("Produce a long report.")
    events = []

    result = agent_loop(
        conversation=conversation,
        provider=StaticProvider(ChatResponse(
            content="Partial report",
            model="fake",
            usage={"input_tokens": 10, "output_tokens": 8192},
            finish_reason="max_tokens",
        )),
        tool_registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        max_turns=1,
        on_event=events.append,
    )

    assert result.response_text == "Partial report"
    assert result.num_turns == 1
    assert result.usage == {"input_tokens": 10, "output_tokens": 8192}
    assert conversation.messages[-1].role == "assistant"
    assert conversation.messages[-1].content == "Partial report"
    final_event = events[-1]
    assert isinstance(final_event, ResultEvent)
    assert final_event.subtype == "error_max_tokens"
    assert final_event.is_error is True
    assert final_event.result == "Partial report"
    assert final_event.errors


def test_max_tokens_does_not_execute_or_store_tool_calls(tmp_path):
    conversation = Conversation()
    conversation.add_user_message("Run the counter.")
    tool = CountingTool()
    events = []

    result = agent_loop(
        conversation=conversation,
        provider=StaticProvider(ChatResponse(
            content="",
            model="fake",
            usage={},
            finish_reason="max_tokens",
            tool_uses=[{
                "id": "truncated-call",
                "name": "Counter",
                "input": {},
            }],
        )),
        tool_registry=ToolRegistry([tool]),
        tool_context=ToolContext(workspace_root=tmp_path),
        max_turns=1,
        on_event=events.append,
    )

    assert result.response_text == ""
    assert tool.executions == 0
    assert not any(
        isinstance(block, ToolUseContentBlock)
        for message in conversation.messages
        if isinstance(message.content, list)
        for block in message.content
    )
    final_event = events[-1]
    assert isinstance(final_event, ResultEvent)
    assert final_event.subtype == "error_max_tokens"
    assert final_event.is_error is True
