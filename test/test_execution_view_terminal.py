"""Real terminal interaction; only the agent workload is replaced with a fixture."""

import os
import sys

import pytest

pexpect = pytest.importorskip('pexpect')

PROGRAM = r'''
import sys, time
from unittest.mock import Mock, patch
from rich.console import Console
from biopaster.repl.core import BioPasterStreamingREPL, build_prompt_session
from biopaster.agent.conversation import Conversation
from biopaster.agent.agent_loop import ToolEvent, ResultEvent
from biopaster.tool_system.permissions import PermissionRequest

repl = BioPasterStreamingREPL.__new__(BioPasterStreamingREPL)
repl.console = Console()
repl.conversation = Conversation()
repl.session_log = Mock()
repl.multiline_mode = False
repl.stream = True
repl._execution_view = None
repl._current_status = None
repl.commands = ['/help', '/permissions', '/multiline', '/quit']
repl.prompt_session = build_prompt_session(repl.commands)
repl.provider = repl.tool_registry = repl.tool_context = None
mode = sys.argv[1]
turn = 0

def agent(**kw):
    global turn
    turn += 1
    kw['on_text_chunk']('Checking sample groups.')
    kw['on_event'](ToolEvent('tool_use', 'Read', tool_input={'file_path': 'counts.csv'}, tool_use_id='a'))
    if mode == 'interrupt':
        time.sleep(60)
        raise AssertionError('Cancellation failed')
    elif mode == 'permission':
        answer = repl._ask_permission(PermissionRequest('Read', 'a', 'READ_APPROVAL', (), ()))
        assert answer.allowed
    elif mode == 'exception':
        raise RuntimeError('SIMULATED_FAILURE')
    else:
        time.sleep(1)
    kw['on_event'](ToolEvent('tool_result', 'Read', tool_output='SIX_SAMPLES', tool_use_id='a'))
    text = f'**ANALYSIS_DONE_{turn}**'
    kw['on_text_chunk'](text)
    kw['on_event'](ResultEvent('success', False, text, [], 1))
    return Mock(response_text=text)

try:
    with patch('biopaster.repl.core.agent_loop', agent):
        repl._run_loop()
finally:
    repl._close_execution_view()
print('CLEAN_EXIT')
'''


def spawn(mode):
    return pexpect.spawn(sys.executable, ['-c', PROGRAM, mode], encoding='utf-8',
                         env=dict(os.environ, TERM='xterm-256color', PYTHONDONTWRITEBYTECODE='1'),
                         dimensions=(32, 100), timeout=10)


def begin(child):
    child.expect('❯')
    child.send('\x1b[1;1R')
    child.sendline('Analyze samples')


def test_multiline_mode_survives_completion_and_next_turn():
    child = spawn('success')
    try:
        child.expect('❯')
        child.send('\x1b[1;1R')
        child.sendline('/multiline')
        child.expect(r'\.\.\. ')
        child.send('Analyze\nsamples\x1b\r')
        child.expect('ANALYSIS_DONE_1')
        child.send('Again\r')
        child.send('\x0f')
        child.expect('Ctrl\\+O to collapse')
        child.send('\x1b\r')
        child.expect('ANALYSIS_DONE_2')
        child.send('/quit\x1b\r')
        child.expect('CLEAN_EXIT')
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)


def test_two_turns_completion_help_resize_and_toggle_without_fullscreen():
    child = spawn('success')
    chunks = []
    try:
        begin(child)
        child.expect('Running')
        child.sendcontrol('o')
        child.expect('Ctrl\\+O to collapse')
        child.setwinsize(24, 65)
        child.expect('ANALYSIS_DONE_1')
        chunks.append(child.before + child.after)
        child.sendcontrol('o')
        child.expect('Checking sample groups')
        chunks.append(child.before + child.after)
        child.sendcontrol('o')
        child.sendline('/help')
        child.expect('Toggle multiline input mode')
        child.expect('❯')
        child.sendline('Again')
        child.expect('ANALYSIS_DONE_2')
        chunks.append(child.before + child.after)
        child.sendline('/quit')
        child.expect('CLEAN_EXIT')
        chunks.append(child.before + child.after)
        child.expect(pexpect.EOF)
        assert '\x1b[?1049h' not in ''.join(chunks)
        assert '**ANALYSIS_DONE' not in ''.join(chunks)
    finally:
        child.close(force=True)
    assert child.exitstatus == 0


@pytest.mark.parametrize('mode', ['permission', 'interrupt', 'exception'])
def test_permission_interrupt_and_error_return_to_original_input(mode):
    child = spawn(mode)
    try:
        begin(child)
        if mode == 'permission':
            child.expect('Choose')
            child.sendcontrol('o')
            child.sendline('1')
            child.expect('ANALYSIS_DONE_1')
        elif mode == 'interrupt':
            child.expect('Running')
            child.sendcontrol('c')
            child.expect('Interrupted')
        else:
            child.expect('SIMULATED_FAILURE')
        child.sendline('/quit')
        child.expect('CLEAN_EXIT')
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)
    assert child.exitstatus == 0
