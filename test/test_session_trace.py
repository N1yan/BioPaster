"""Offline tests for compact, non-repeating execution logs."""
import importlib
import json
from types import SimpleNamespace

import pytest

from biopaster.agent.conversation import Conversation, Message
from biopaster.agent.session_log import SessionLog
from biopaster.compact.auto_compact import AutoCompactResult
from biopaster.providers.base import ChatResponse, ToolCallFormatError
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.protocol import ToolResult

loop = importlib.import_module('biopaster.agent.agent_loop')


def events(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


def response(text='done', reason='end_turn', calls=None):
    return ChatResponse(text, 'test', {'input_tokens': 2, 'output_tokens': 1}, reason,
                        tool_uses=calls)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    log = SessionLog(tmp_path / 'sessions', enabled=True)
    context = ToolContext(tmp_path, session_log=log)
    conversation = Conversation()
    conversation.add_user_message('user-task-marker')
    monkeypatch.setattr(loop, 'assemble_system_prompt', lambda _: 'fixed-system-marker')
    monkeypatch.setattr(loop, 'auto_compact_messages', lambda **kw: AutoCompactResult(kw['messages'], False))
    monkeypatch.setattr(loop.LoopDetector if hasattr(loop, 'LoopDetector') else type(conversation.loop_detector),
                        'check', lambda *args: (None, {}, False))
    registry = SimpleNamespace(list_specs=lambda: [], dispatch=lambda *args: ToolResult(name='Read', output=[{'type': 'text', 'content': 'result-marker', 'metadata': {}}]))
    return log, context, conversation, registry


def run(runtime, replies, **kwargs):
    log, context, conversation, registry = runtime
    iterator = iter(replies)
    def chat(*args, **kw):
        item = next(iterator)
        if isinstance(item, BaseException):
            raise item
        return item
    provider = SimpleNamespace(max_output_tokens=100, context_window=10000, chat_stream_response=chat)
    return loop.agent_loop(conversation, provider, registry, context, **kwargs)


def test_request_drops_context_and_response_drops_body(tmp_path):
    log = SessionLog(tmp_path, enabled=True)
    log.record_model_request('id', {'model': 'test', 'messages': ['history-marker'],
        'system': 'system-marker', 'tools': ['schema-marker'], 'max_tokens': 10,
        'extra_body': {'chat_template_kwargs': {'enable_thinking': True}}})
    log.record_model_response('id', response('body-marker'))
    raw = log.path.read_text()
    assert all(s not in raw for s in ['history-marker', 'system-marker', 'schema-marker', 'body-marker'])
    assert events(log)[1]['data']['settings']['max_tokens'] == 10


def test_each_content_written_once_without_ui(runtime):
    call = {'id': 't1', 'name': 'Read', 'input': {'path': 'argument-marker'}}
    result = run(runtime, [response('planning-marker', 'tool_use', [call]), response('final-marker')])
    log = runtime[0]
    raw = log.path.read_text()
    for value in ['user-task-marker', 'argument-marker', 'planning-marker', 'result-marker', 'final-marker']:
        assert raw.count(value) == 1
    assert result.subtype == 'success'
    records = events(log)
    assert records[-1]['type'] == 'run_end'
    assert all(e['run_id'] for e in records[1:])


@pytest.mark.parametrize('first,reason', [
    (response('', 'end_turn'), 'empty_response_recovery'),
    (response('partial', 'max_tokens'), 'output_token_recovery'),
    (ToolCallFormatError('invalid', response=response(), details={'broken': True}), 'tool_call_format_recovery'),
])
def test_recovery_records_actual_inserted_message(runtime, first, reason):
    run(runtime, [first, response()])
    records = events(runtime[0])
    injected = [e['data'] for e in records if e['type'] == 'injected_message']
    assert len(injected) == 1 and injected[0]['reason'] == reason
    assert any(m.content == injected[0]['content'] for m in runtime[2].messages)


def test_loop_feedback_and_compaction(runtime, monkeypatch):
    log, context, conversation, _ = runtime
    monkeypatch.setattr(type(conversation.loop_detector), 'check', lambda *args: ('no progress', {}, True))
    context.task_store = SimpleNamespace(list_tasks=lambda: [1])
    count = 0
    def compact(**kw):
        nonlocal count
        count += 1
        return AutoCompactResult([Message('user', 'summary-marker')], True) if count == 2 else AutoCompactResult(kw['messages'], False)
    monkeypatch.setattr(loop, 'auto_compact_messages', compact)
    run(runtime, [response('try', 'tool_use', [{'id':'a','name':'Read','input':{}}]), response()])
    records = events(log)
    injections = [e['data'] for e in records if e['type']=='injected_message']
    assert {d['reason'] for d in injections} == {'loop_recovery', 'task_state_after_compaction'}
    assert log.path.read_text().count('summary-marker') == 1
    assert any(e['type']=='context_compacted' and e['data']['replaced_message_count']==4 for e in records)


def test_partial_response_survives_interrupt_and_resume(runtime):
    log, context, conversation, registry = runtime
    def chat(*args, **kw):
        kw['on_text_chunk']('partial-marker')
        raise KeyboardInterrupt()
    provider = SimpleNamespace(max_output_tokens=100, context_window=10000, chat_stream_response=chat)
    with pytest.raises(KeyboardInterrupt):
        loop.agent_loop(conversation, provider, registry, context)
    assert events(log)[-1]['data']['status']=='interrupted'
    assert log.path.read_text().count('partial-marker')==1
    old = log.path.read_text()
    reopened = SessionLog.open_existing(log.directory.parent, log.session_id, enabled=True)
    context.session_log = reopened
    run((reopened, context, conversation, registry), [response()])
    assert reopened.path.read_text().startswith(old)
    assert reopened.path.read_text().count('user-task-marker') == 1
    assert events(reopened)[-1]['data']['status']=='success'


def test_disabled_log_writes_nothing(tmp_path):
    log = SessionLog(tmp_path, enabled=False)
    log.start_run('task')
    log.record_model_request('id', {'messages':['history'], 'model':'test'})
    log.record_model_response('id', response())
    log.end_run('success')
    assert not log.path.exists()


def test_tool_failure_records_error_and_parameters_once(runtime):
    def fail(*args):
        raise ValueError('failure-marker')
    runtime[3].dispatch = fail
    run(runtime, [response('try', 'tool_use', [{'id':'a','name':'Read','input':{'path':'bad-path-marker'}}]), response()])
    raw = runtime[0].path.read_text()
    assert raw.count('bad-path-marker')==1
    assert raw.count('failure-marker')==1
    assert any(e['type']=='tool_error' for e in events(runtime[0]))


def test_disabled_agent_log_does_not_change_result(runtime):
    log, context, conversation, registry = runtime
    disabled = SessionLog(log.directory.parent, enabled=False)
    context.session_log = disabled
    assert run((disabled,context,conversation,registry), [response()]).subtype=='success'
    assert not disabled.path.exists()


def test_openai_provider_logs_metadata_only(tmp_path):
    from biopaster.providers.openai_provider import OpenAIProvider
    provider = OpenAIProvider.__new__(OpenAIProvider)
    log = SessionLog(tmp_path, enabled=True)
    provider.session_log = log
    provider.validate_configuration = lambda: None
    provider._build_request = lambda *a, **k: {'model':'test','messages':['history-marker'],'tools':['tool-schema-marker']}
    provider._apply_thinking_settings = lambda *a: None
    async def send(*a, **k):
        return response('answer-marker')
    provider._send_request = send
    result = provider.chat_stream_response([])
    assert result.content=='answer-marker'
    raw=log.path.read_text()
    assert all(s not in raw for s in ['history-marker','tool-schema-marker','answer-marker'])
    assert [e['type'] for e in events(log)][1:]==['model_request','model_response']


def test_anthropic_stream_has_no_fragment_log(tmp_path):
    from biopaster.providers.anthropic_provider import AnthropicProvider
    provider=AnthropicProvider.__new__(AnthropicProvider)
    log=SessionLog(tmp_path,enabled=True)
    provider.session_log=log
    chunks=[]
    provider._record_stream_event(SimpleNamespace(type='text',text='chunk-marker'),'id',chunks.append)
    assert chunks==['chunk-marker']
    provider._build_chat_response=lambda m: response('answer-marker')
    from anthropic.types import Message as APIMessage
    message=APIMessage(id='msg',type='message',role='assistant',model='test',
        content=[{'type':'text','text':'answer-marker'}],stop_reason='end_turn',
        usage={'input_tokens':2,'output_tokens':1})
    result=provider._finish_message(message,None,'id')
    assert result.content=='answer-marker'
    raw=log.path.read_text()
    assert 'chunk-marker' not in raw and 'answer-marker' not in raw
    assert events(log)[-1]['data']['usage']['input_tokens']==2
