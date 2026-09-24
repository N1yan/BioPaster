import threading
import time
from contextlib import contextmanager

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from biopaster.agent.agent_loop import ToolEvent
from biopaster.repl.execution_view import ExecutionView
from biopaster.tool_system.permissions import PermissionRequest, PermissionRule


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@contextmanager
def view_session(*, multiline=False):
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput(),
            completer=FuzzyCompleter(WordCompleter(['/help', '/permissions', '/quit'])),
            style=Style.from_dict({'prompt': 'bold cyan'}))
        original_buffer = session.default_buffer
        original_layout = session.app.layout.container
        original_accept = original_buffer.accept_handler
        interrupted = threading.Event()
        view = ExecutionView(session, multiline=multiline, interrupt=interrupted.set)
        view.start()
        try:
            yield view, session, pipe, interrupted
        finally:
            view.close()
        assert session.default_buffer is original_buffer
        assert session.default_buffer.accept_handler is original_accept
        assert session.app.layout.container is original_layout
        assert not view.thread.is_alive()


def screen(session):
    rendered = session.app.renderer.last_rendered_screen
    if rendered is None:
        return ''
    return '\n'.join(''.join(cell.char for _, cell in sorted(row.items()))
                     for _, row in sorted(rendered.data_buffer.items()))


def test_toggle_during_execution_and_after_finish_keeps_full_history():
    with view_session() as (view, session, pipe, _):
        view.append_text('Checking samples')
        view.tool_event(ToolEvent('tool_use', 'Read', tool_input={'file_path': 'counts.csv'}, tool_use_id='a'))
        view.tool_event(ToolEvent('tool_result', 'Read', tool_output='SIX_SAMPLES', tool_use_id='a'))
        pipe.send_text('\x0f')
        wait_for(lambda: view.expanded and 'Read · Completed' in screen(session))
        view.append_text('Now running analysis')
        wait_for(lambda: 'Now running analysis' in screen(session))
        view.tool_event(ToolEvent('tool_use', 'Bash', tool_input={'command': 'analysis'}, tool_use_id='b'))
        view.tool_event(ToolEvent('tool_result', 'Bash', tool_output='done', tool_use_id='b'))
        pipe.send_text('\x0f')
        wait_for(lambda: not view.expanded)
        view.append_text('FINAL_ANSWER')
        view.finish('FINAL_ANSWER')
        wait_for(lambda: 'FINAL_ANSWER' in screen(session))
        pipe.send_text('\x0f')
        wait_for(lambda: view.expanded and 'Read · Completed' in view.rendered_text)
        assert 'SIX_SAMPLES' not in view.rendered_text
        assert 'counts.csv' not in view.rendered_text
        assert 'Checking samples' in view.rendered_text
        assert view.rendered_text.count('FINAL_ANSWER') == 1
        pipe.send_text('\x0f/quit\r')
        assert view.read_next() == '/quit'


def test_original_inline_prompt_completion_and_editing_survive():
    with view_session() as (view, session, pipe, _):
        view.finish('Done')
        pipe.send_text('/p')
        wait_for(lambda: '/permissions' in screen(session))
        assert '❯ /p' in screen(session)
        cells = session.app.renderer.last_rendered_screen.data_buffer
        assert any('prompt' in c.style for row in cells.values() for c in row.values() if c.char == '❯')
        pipe.send_text('\t\r')
        assert view.read_next() == '/permissions'
    with view_session() as (view, session, pipe, _):
        view.finish('Done')
        pipe.send_text('abc\x01\x04\r')
        assert view.read_next() == 'bc'


@pytest.mark.parametrize('multiline', [False, True])
def test_native_bracketed_paste_and_multiline(multiline):
    with view_session(multiline=multiline) as (view, session, pipe, _):
        view.finish('Done')
        text = '检查样本\n/home/yan/data.csv\n继续分析'
        pipe.send_text('\x1b[200~' + text + '\x1b[201~')
        wait_for(lambda: session.default_buffer.text == text)
        assert view.thread.is_alive()
        pipe.send_text('\x1b\r' if multiline else '\r')
        assert view.read_next() == text


def test_markdown_links_and_tables_are_formatted_before_input_submission():
    with view_session() as (view, session, pipe, _):
        view.finish('**Important**\n\n[results](https://example.org/results.csv)\n\n| Gene | Result |\n|---|---|\n| ERBB2 | up |\n\n```python\nprint(1)\n```')
        wait_for(lambda: 'ERBB2' in view.rendered_text)
        assert '**' not in view.rendered_text and '```' not in view.rendered_text
        assert '8;id=' not in view.rendered_text
        assert 'https://example.org/results.csv' in view.rendered_text
        assert 'print(1)' in view.rendered_text


def test_permission_preserves_input_draft_and_does_not_steal_toggle():
    with view_session() as (view, session, pipe, _):
        pipe.send_text('my next question')
        wait_for(lambda: session.default_buffer.text == 'my next question')
        values = []
        request = PermissionRequest('Bash', 'a', 'Run analysis', (), (PermissionRule('Bash', 'Rscript'),))
        worker = threading.Thread(target=lambda: values.append(view.ask_permission(request)))
        worker.start()
        try:
            wait_for(lambda: view.permission_pending and session.default_buffer.text == '')
            pipe.send_text('\x0finvalid\r')
            wait_for(lambda: 'Please enter a listed number' in view.rendered_text)
            assert not view.expanded
            pipe.send_text('2\r')
            worker.join(3)
            assert values[0].allowed and values[0].rules == request.suggestions
            wait_for(lambda: session.default_buffer.text == 'my next question')
            view.finish('Done')
            pipe.send_text('\r')
            assert view.read_next() == 'my next question'
        finally:
            view.close()
            worker.join(3)


def test_cancel_during_execution_and_empty_input_after_finish():
    with view_session() as (view, session, pipe, interrupted):
        pipe.send_text('\x03')
        assert interrupted.wait(3)
        view.finish(errors=['Interrupted.'])
        pipe.send_text('\r')
        time.sleep(0.15)
        assert view.thread.is_alive()
        pipe.send_text('\x0f')
        wait_for(lambda: view.expanded)
        pipe.send_text('\x0f/quit\r')
        assert view.read_next() == '/quit'


def test_scroll_position_is_retained_while_expanded():
    with view_session() as (view, session, pipe, _):
        view.append_text('\n\n'.join(f'History step {n}' for n in range(100)))
        pipe.send_text('\x0f')
        wait_for(lambda: view.expanded and view.scroll_line > 0)
        pipe.send_text('\x1b[5~')
        wait_for(lambda: not view.follow_tail)
        before = view.scroll_line
        view.append_text('\n\nNew progress')
        wait_for(lambda: 'New progress' in view.rendered_text)
        assert view.scroll_line == before


def test_collapsing_shrinks_process_window_to_content_height():
    with view_session() as (view, session, pipe, _):
        for index in range(25):
            view.tool_event(ToolEvent('tool_use', f'Tool{index}', tool_use_id=str(index)))
        pipe.send_text('\x0f')
        wait_for(lambda: view.expanded and view.window.render_info.window_height >= 15)
        pipe.send_text('\x0f')
        wait_for(lambda: not view.expanded and 'Ctrl+O to expand' in view.rendered_text)
        wait_for(lambda: view.window.render_info.window_height == view._line_count)
        view.finish('Done')
        wait_for(lambda: 'Done' in view.rendered_text)
        wait_for(lambda: view.window.render_info.window_height == view._line_count)
