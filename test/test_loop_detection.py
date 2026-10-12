"""Exercise loop recovery through the real agent loop without model/network calls."""
import importlib
import json
from types import SimpleNamespace

import pytest

from biopaster.agent.conversation import Conversation
from biopaster.agent.session_log import SessionLog
from biopaster.providers.base import ChatResponse
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.protocol import ToolResult


@pytest.fixture
def harness(tmp_path, monkeypatch):
    loop = importlib.import_module("biopaster.agent.agent_loop")
    monkeypatch.setattr(loop, "assemble_system_prompt", lambda context: "Test agent")
    monkeypatch.setattr(loop, "micro_compact_messages", lambda messages, **kw: messages)
    monkeypatch.setattr(loop, "auto_compact_messages", lambda **kw: SimpleNamespace(
        messages=kw["messages"], was_compacted=False))
    context = ToolContext(workspace_root=tmp_path)
    context.session_log = SessionLog(tmp_path / "sessions")

    class Registry:
        calls = 0
        fail = True

        def list_specs(self):
            return []

        def dispatch(self, call, context):
            self.calls += 1
            return ToolResult(call.name, [{"type": "text", "content": "Unavailable method" if self.fail else "Running", "metadata": {}}], is_error=self.fail)

    registry = Registry()
    conversation = Conversation()
    conversation.add_user_message("Complete the task.")

    def run(provider, turns=20, conv=None):
        events = []
        result = loop.agent_loop(conv or conversation, provider, registry, context,
                                 max_turns=turns, on_event=events.append)
        return result, events

    return SimpleNamespace(run=run, conversation=conversation, registry=registry, loop=loop)


class Provider:
    context_window = 128000
    max_output_tokens = 8192

    def __init__(self, count=20, varied=False, checks=None, batch_size=1):
        self.turns = 0
        self.count = count
        self.varied = varied
        self.checks = list(checks or [])
        self.check_turns = []
        self.batch_size = batch_size
        self.requests = []

    def chat_stream_response(self, messages, **kwargs):
        if kwargs["system"] != "Test agent":
            self.check_turns.append(self.turns)
            assert not kwargs.get("tools")
            assert kwargs["max_tokens"] <= 1024
            assert kwargs["timeout"] <= 60
            assert kwargs["max_retries"] == 0
            answer = self.checks.pop(0) if self.checks else '{"is_stuck":false,"reason":"Progress continues","turns":[]}'
            if isinstance(answer, Exception):
                raise answer
            return ChatResponse(answer, "fake", {"output_tokens": 7}, "end_turn")
        self.requests.append(messages)
        self.turns += 1
        if self.turns > self.count:
            return ChatResponse("Done", "fake", {}, "end_turn")
        return ChatResponse("Trying the operation", "fake", {}, "tool_use", tool_uses=[
            {"id": f"{self.turns}-{i}", "name": "ExecuteCode",
             "input": {"code": f"attempt({self.turns if self.varied else 0})"}}
            for i in range(self.batch_size)
        ])


def feedback(conversation):
    return [m.content for m in conversation.messages
            if isinstance(m.content, str) and m.content.startswith("[Runtime feedback]")]


def test_repeated_failures_warn_once_then_stop(harness):
    result, events = harness.run(Provider())
    assert harness.registry.calls == 6
    assert result.subtype == "stuck"
    assert events[-1].subtype == "stuck"
    assert len(feedback(harness.conversation)) == 1
    assert feedback(harness.conversation)[0].count("Review the previous attempts") == 3
    assert "Unavailable method" in feedback(harness.conversation)[0]
    assert harness.conversation.to_dict()["loop_detector"]["status"] == "stuck"


def test_feedback_follows_all_results_in_multi_tool_turn(harness):
    result, _ = harness.run(Provider(batch_size=4))
    assert harness.registry.calls == 8
    messages = harness.conversation.get_messages()
    index = next(i for i, m in enumerate(messages) if isinstance(m["content"], str)
                 and m["content"].startswith("[Runtime feedback]"))
    assert len(messages[index - 1]["content"]) == 4
    assert all(b["type"] == "tool_result" for b in messages[index - 1]["content"])
    assert result.subtype == "stuck"


def test_successful_polling_is_not_a_rule_loop(harness):
    harness.registry.fail = False
    provider = Provider(count=11)
    result, _ = harness.run(provider)
    assert result.response_text == "Done"
    assert not feedback(harness.conversation)
    assert provider.check_turns == [5, 10]
    assert result.usage["output_tokens"] == 14


def test_semantic_confirmation_requires_new_turns_after_feedback(harness):
    checks = [json.dumps({"is_stuck": True, "reason": "Equivalent failing attempts", "turns": [1, 3, 5]}),
              json.dumps({"is_stuck": True, "reason": "Still repeating after feedback", "turns": [6, 8, 10]})]
    provider = Provider(varied=True, checks=checks)
    result, _ = harness.run(provider)
    assert result.subtype == "stuck"
    assert harness.registry.calls == 10
    assert provider.check_turns == [5, 10]
    assert len(feedback(harness.conversation)) == 1


@pytest.mark.parametrize("bad", [TimeoutError("slow checker"), "not JSON",
    '{"is_stuck":"true","reason":"bad type","turns":[1,2,3]}',
    '{"is_stuck":true,"reason":"invented evidence","turns":[99,100]}'])
def test_checker_failure_does_not_stop_task(harness, bad):
    result, _ = harness.run(Provider(count=6, varied=True, checks=[bad]))
    assert result.response_text == "Done"
    assert not feedback(harness.conversation)


def test_recovery_success_starts_next_task_with_fresh_state(harness):
    result, _ = harness.run(Provider(count=3))
    assert result.response_text == "Done"
    assert len(feedback(harness.conversation)) == 1
    harness.conversation.add_user_message("Another task.")
    result, _ = harness.run(Provider(count=3))
    assert result.response_text == "Done"
    assert len(feedback(harness.conversation)) == 2


def test_interrupted_task_retains_warning_across_restore_and_compaction(harness):
    class InterruptedProvider(Provider):
        def chat_stream_response(self, messages, **kwargs):
            if self.turns == 3:
                raise KeyboardInterrupt()
            return super().chat_stream_response(messages, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        harness.run(InterruptedProvider())
    restored = Conversation.from_dict(json.loads(json.dumps(harness.conversation.to_dict())))
    restored.messages = restored.messages[-1:]
    restored.add_user_message("Continue.")
    result, _ = harness.run(Provider(), conv=restored)
    assert result.subtype == "stuck"
    assert harness.registry.calls == 6
    assert len(feedback(restored)) == 1


def test_old_sessions_without_detector_still_load():
    conversation = Conversation.from_dict({"messages": []})
    assert conversation.messages == []


def test_success_between_failures_breaks_exact_repeat_streak(harness):
    dispatch = harness.registry.dispatch

    def alternating(call, context):
        harness.registry.fail = harness.registry.calls % 3 != 2
        return dispatch(call, context)

    harness.registry.dispatch = alternating
    result, _ = harness.run(Provider(count=8))
    assert result.response_text == "Done"
    assert not feedback(harness.conversation)


def test_different_structured_errors_are_not_identical(harness):
    def dispatch(call, context):
        harness.registry.calls += 1
        return ToolResult(call.name, [{"type": "text", "content": {
            "stderr": f"Failure {harness.registry.calls}", "exit_code": 1}, "metadata": {}}], is_error=True)

    harness.registry.dispatch = dispatch
    result, _ = harness.run(Provider(count=8))
    assert result.response_text == "Done"
    assert not feedback(harness.conversation)


def test_canonical_arguments_ignore_dictionary_key_order(harness):
    class ReorderedProvider(Provider):
        def chat_stream_response(self, messages, **kwargs):
            result = super().chat_stream_response(messages, **kwargs)
            for call in result.tool_uses or []:
                call["input"] = {"x": 1, "y": 2} if self.turns % 2 else {"y": 2, "x": 1}
            return result

    result, _ = harness.run(ReorderedProvider())
    assert result.subtype == "stuck"
    assert harness.registry.calls == 6


def test_second_semantic_check_cannot_cite_pre_warning_turns(harness):
    checks = ['{"is_stuck":true,"reason":"Repeating","turns":[1,3,5]}',
              '{"is_stuck":true,"reason":"Old repetition","turns":[1,3,5]}']
    result, _ = harness.run(Provider(count=10, varied=True, checks=checks))
    assert result.response_text == "Done"
    assert len(feedback(harness.conversation)) == 1
    assert "available turns" in harness.conversation.loop_detector.diagnostic


def test_different_images_do_not_become_identical_failures(harness):
    def dispatch(call, context):
        harness.registry.calls += 1
        return ToolResult(call.name, [
            {"type": "text", "content": "Failed", "metadata": {}},
            {"type": "image", "content": str(harness.registry.calls), "metadata": {"media_type": "image/png"}},
        ], is_error=True)

    harness.registry.dispatch = dispatch
    result, _ = harness.run(Provider(count=3))
    assert result.response_text == "Done"
    assert not feedback(harness.conversation)
