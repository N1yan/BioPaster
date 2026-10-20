"""Replay must survive compaction without changing model messages."""

from io import StringIO
from types import SimpleNamespace
import json

import pytest
from rich.console import Console

from biopaster.agent.agent_loop import AgentLoopResult, ToolEvent
from biopaster.agent.session import load_session
from biopaster.agent import session as session_storage
from biopaster.repl import core


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(core, "load_config", lambda: {})
    monkeypatch.setattr(core, "resolve_model_config", lambda _: {"provider": "test"})
    monkeypatch.setattr(core, "get_configured_notebook_kernels", lambda: {})
    monkeypatch.setattr(core, "build_prompt_session", lambda _: object())
    monkeypatch.setattr(core.BioPasterStreamingREPL, "_build_provider",
                        lambda *args: SimpleNamespace(model="test"))
    monkeypatch.setattr(core, "Console", lambda **kwargs: Console(**{"file": StringIO(), **kwargs}))
    monkeypatch.setattr(core, "generate_session_title", lambda *args: "Test")

    class View:
        def __init__(self, *args, **kwargs):
            self.running, self.result, self.errors, self.failure = True, "", [], None

        def start(self):
            pass

        def refresh_tasks(self):
            pass

        def append_text(self, text):
            pass

        def tool_event(self, event):
            pass

        def finish(self, result="", errors=None):
            self.running, self.result, self.errors = False, result, errors or []

        def close(self):
            pass

    monkeypatch.setattr(core, "ExecutionView", View)
    return core.BioPasterStreamingREPL(workspace_root=tmp_path)


def test_replay_survives_two_compactions_and_resume(app, monkeypatch, tmp_path):
    received = []

    def run(**kwargs):
        conversation = kwargs["conversation"]
        pending = load_session(app.session_log.session_id).replay_history.turns[-1]
        assert pending.user == conversation.messages[-1].content
        assert pending.status == "pending"
        received.append(conversation.get_messages())
        kwargs["on_text_chunk"]("Internal progress")
        kwargs["on_event"](ToolEvent("tool_use", "Search"))
        conversation.messages.clear()
        conversation.add_user_message("[Compacted]\nInternal summary")
        answer = f"Final answer {len(received)}"
        conversation.add_assistant_message(answer)
        kwargs["on_text_chunk"](answer)
        kwargs["on_checkpoint"]()
        return AgentLoopResult(answer)

    monkeypatch.setattr(core, "agent_loop", run)
    app.chat("Original request")
    app.chat("Follow-up request")
    saved_id = app.session_log.session_id
    snapshot = load_session(saved_id)
    assert len(snapshot.conversation.messages) == 2
    assert received[1] == [
        {"role": "user", "content": "[Compacted]\nInternal summary"},
        {"role": "assistant", "content": "Final answer 1"},
        {"role": "user", "content": "Follow-up request"},
    ]
    restored = core.BioPasterStreamingREPL(workspace_root=tmp_path)
    restored._resume_session(saved_id)
    output = restored.console.file.getvalue()
    for text in ("Original request", "Follow-up request", "Final answer 1", "Final answer 2"):
        assert output.count(text) == 1
    assert "Internal summary" not in output
    assert "Internal progress" not in output
    assert restored.conversation.to_dict() == snapshot.conversation.to_dict()
    restored._new_session()
    assert not restored.replay_history.turns


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_turn_keeps_user_and_partial_answer(app, monkeypatch, failure):
    def run(**kwargs):
        kwargs["on_text_chunk"]("Earlier commentary")
        kwargs["on_event"](ToolEvent("tool_use", "Search"))
        kwargs["on_text_chunk"]("Partial answer")
        raise failure("Stopped")

    monkeypatch.setattr(core, "agent_loop", run)
    app.chat("Keep my exact request")
    snapshot = load_session(app.session_log.session_id)
    turn = snapshot.replay_history.turns[0]
    assert turn.user == "Keep my exact request"
    assert turn.answer == "Partial answer"
    assert turn.status == ("interrupted" if failure is KeyboardInterrupt else "error")
    assert len(snapshot.replay_history.turns) == 1


def test_session_requires_replay_history(app):
    app.conversation.add_user_message("[Compacted]\nInternal summary")
    app._save_session_snapshot()
    path = app.session_log.directory / "session.json"
    data = json.loads(path.read_text())
    del data["replay_history"]
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="requires replay history"):
        load_session(app.session_log.session_id)


def test_failed_snapshot_write_keeps_previous_record_and_allows_retry(app, monkeypatch):
    monkeypatch.setattr(core, "agent_loop", lambda **kwargs: AgentLoopResult("Final answer"))
    app.chat("Original request")
    path = app.session_log.directory / "session.json"
    saved = path.read_bytes()
    app.replay_history.turns[0].answer = "Updated answer"
    original_replace = session_storage.os.replace

    def fail_replace(*args):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(session_storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="Disk unavailable"):
        app._save_session_snapshot()
    assert path.read_bytes() == saved
    assert app.replay_history.turns[0].answer == "Updated answer"
    monkeypatch.setattr(session_storage.os, "replace", original_replace)
    app._save_session_snapshot()
    assert load_session(app.session_log.session_id).replay_history.turns[0].answer == "Updated answer"


def test_resize_history_keeps_prior_turns_and_leaves_current_answer_to_live_view(app, monkeypatch):
    answers = iter(("Earlier final answer", "Current final answer"))
    monkeypatch.setattr(core, "agent_loop",
                        lambda **kwargs: AgentLoopResult(next(answers)))
    app.chat("Earlier user request")
    app.chat("Current user request")
    before = app.conversation.to_dict()
    for width in (40, 100):
        rendered = app._render_resize_history(width)
        assert rendered.count("Earlier user request") == 1
        assert rendered.count("Earlier final answer") == 1
        assert rendered.count("Current user request") == 1
        assert "Current final answer" not in rendered
    assert app.conversation.to_dict() == before
