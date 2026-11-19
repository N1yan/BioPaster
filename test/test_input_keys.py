"""Exercise the terminal parser and input bindings without a live terminal."""
import asyncio

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.application import create_app_session

from biopaster.repl.core import build_prompt_session
from biopaster.repl.execution_view import ExecutionView


def prompt(keys, *, multiline=True, execution=False):
    async def exercise(pipe):
        session=build_prompt_session(['/help','/exit'])
        view=ExecutionView(session) if execution else None
        kwargs={'multiline':multiline}
        if view:
            kwargs['key_bindings']=view._bindings(session.key_bindings)
        task=asyncio.create_task(session.prompt_async('> ', **kwargs))
        await asyncio.sleep(.05)
        pipe.send_text(keys)
        return await asyncio.wait_for(task, 2)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            return asyncio.run(exercise(pipe))


@pytest.mark.parametrize('key',['\n','\x1b\r'])
@pytest.mark.parametrize('execution',[False,True])
def test_newline_then_enter_sends_whole_message(key, execution):
    assert prompt('first'+key+'second\r',execution=execution)=='first\nsecond'


@pytest.mark.parametrize('key',['\n','\x1b\r'])
def test_choices_do_not_accept_or_insert_on_newline(key):
    assert prompt('1'+key+'2\r',multiline=False,execution=True)=='12'


def test_pasted_multiline_stays_in_buffer_until_enter():
    assert prompt('\x1b[200~one\ntwo\x1b[201~\r')=='one\ntwo'


def test_plain_enter_and_slash_command():
    assert prompt('/help\r')=='/help'
