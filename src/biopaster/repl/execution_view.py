"""Fold one request's progress above the existing PromptSession input."""

from __future__ import annotations

from concurrent.futures import Future
from io import StringIO
import json
import os
import signal
import threading
import time

from prompt_toolkit.completion import DynamicCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines, fragment_list_to_text
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import FloatContainer, HSplit, Window
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from biopaster.agent.agent_loop import ToolEvent
from biopaster.tool_system.permissions import PermissionAnswer


class ExecutionView:
    """Keep native editing/completion and add only a render region and bindings.

    The agent stays on the main thread, preserving its KeyboardInterrupt path.
    This thread runs the SAME PromptSession, including its original buffer,
    completion menu, key bindings, history and input processors. All temporary
    changes are restored when the next question is submitted or the view closes.
    """

    def __init__(self, session, *, multiline=False, interrupt=None):
        self.session = session
        self._container = next(c for c in walk(session.app.layout.container)
                               if isinstance(c, FloatContainer))
        self.multiline = multiline
        self.expanded = False
        self.running = True
        self.entries = []
        self.result = ""
        self.errors = []
        self.status = "Thinking..."
        self.notice = ""
        self.scroll_line = 0
        self.follow_tail = True
        self.rendered_text = ""
        self._revision = 0
        self._render_key = None
        self._fragments = []
        self._line_count = 1
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._closed = False
        self._closing = False
        self.failure = None
        self._next_input = Future()
        self._permission = None
        self._permission_future = None
        self._draft = Document("")
        self._interrupt = interrupt or (lambda: os.kill(os.getpid(), signal.SIGINT))
        self.thread = threading.Thread(target=self._run, name="biopaster-input", daemon=True)
        self.control = FormattedTextControl(self._render, focusable=False,
                                           get_cursor_position=lambda: Point(0, self.scroll_line))
        self.window = Window(self.control, wrap_lines=False, always_hide_cursor=True,
                             height=self._height, dont_extend_height=True,
                             right_margins=[ScrollbarMargin()])

    @property
    def permission_pending(self):
        return self._permission is not None

    def _height(self):
        rows = self.session.app.output.get_size().rows
        return Dimension(min=1, max=max(1, rows - 5 if not self.running else rows // 2))

    def _changed(self):
        self._revision += 1
        self.session.app.invalidate()

    def append_text(self, chunk):
        if not chunk:
            return
        with self._lock:
            if not self.entries or self.entries[-1]["kind"] != "text":
                self.entries.append({"kind": "text", "text": ""})
            self.entries[-1]["text"] += chunk
            self.status = "Preparing response..."
            self.notice = ""
            self._changed()

    def tool_event(self, event: ToolEvent):
        with self._lock:
            if event.kind == "tool_use":
                self.entries.append({"kind": "tool", "id": event.tool_use_id,
                                     "name": event.tool_name, "input": event.tool_input,
                                     "output": None, "status": "Running"})
                self.status = f"{event.tool_name} · Running..."
            elif event.kind in {"tool_result", "tool_error"}:
                entry = next((e for e in reversed(self.entries) if e["kind"] == "tool"
                              and e["id"] == event.tool_use_id and e["name"] == event.tool_name), None)
                if entry is None:
                    entry = {"kind": "tool", "id": event.tool_use_id, "name": event.tool_name, "input": event.tool_input}
                    self.entries.append(entry)
                entry["status"] = "Failed" if event.is_error or event.kind == "tool_error" else "Completed"
                entry["output"] = event.error if event.kind == "tool_error" else event.tool_output
                self.status = f"{event.tool_name} · {entry['status']}. Continuing..."
            self.notice = ""
            self._changed()

    def finish(self, result="", errors=None):
        with self._lock:
            if result and self.entries and self.entries[-1]["kind"] == "text" and self.entries[-1]["text"] == result:
                self.entries.pop()
            self.result = result
            self.errors = list(errors or [])
            self.running = False
            self.expanded = False
            self.follow_tail = False
            self.scroll_line = 0
            self.status = "Failed" if self.errors else "Completed"
            self._changed()

    @staticmethod
    def _literal(value):
        return Text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def _permission_lines(self):
        request = self._permission
        lines = ["Permission required", f"Tool: {request.tool_name}", request.description]
        if request.review_error:
            lines += ["Automatic permission review failed.", request.review_error]
        for target in request.targets:
            lines.append(f"- {target.tool_name}: {target.rule_content if target.rule_content is not None else 'entire tool'}")
        lines.append("1. Allow once")
        for index, rule in enumerate(request.suggestions, 2):
            scope = rule.rule_content if rule.rule_content is not None else "entire tool"
            lines.append(f"{index}. Allow once and remember for this session: {rule.tool_name} ({scope})")
        lines.append("0. Deny")
        return "\n".join(lines)

    def _render(self):
        with self._lock:
            width = max(1, self.session.app.output.get_size().columns - 1)
            frame = int(time.monotonic() * 10) % 10 if self.running and not self.permission_pending else -1
            key = (self._revision, self.expanded, width, frame)
            if key != self._render_key:
                parts = [Text("BioPaster :", style="bold cyan")]
                if self.permission_pending:
                    parts.append(Text(self._permission_lines()))
                else:
                    entries = self.entries if self.expanded else self.entries[-4:] if self.running else []
                    for entry in entries:
                        if entry["kind"] == "text":
                            parts.append(Markdown(entry["text"], hyperlinks=False))
                        else:
                            parts.append(Text(f"{entry['name']} · {entry['status']}", style="red" if entry["status"] == "Failed" else "dim"))
                            if entry["status"] == "Failed" and entry.get("output") is not None:
                                parts.append(self._literal(entry["output"]))
                    if self.running:
                        parts.append(Text(f"{'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[frame]} {self.status}", style="cyan"))
                    label = "▼ Execution details · Ctrl+O to collapse" if self.expanded else "▶ Execution details · Ctrl+O to expand"
                    parts.append(Text(label, style="dim"))
                    if self.expanded:
                        parts.append(Text("PgUp/PgDn to scroll", style="dim"))
                    if self.result:
                        parts.extend([Text(""), Markdown(self.result, hyperlinks=False)])
                    parts.extend(Text(error, style="red") for error in self.errors)
                if self.notice:
                    parts.append(Text(self.notice, style="yellow"))
                output = StringIO()
                Console(file=output, force_terminal=True, color_system="truecolor", width=width).print(Group(*parts))
                self._fragments = to_formatted_text(ANSI(output.getvalue().rstrip("\n")))
                lines = list(split_lines(self._fragments))
                self._line_count = len(lines)
                self.rendered_text = "\n".join(fragment_list_to_text(line) for line in lines)
                self._render_key = key
            self.scroll_line = self._line_count - 1 if self.follow_tail else min(self.scroll_line, self._line_count - 1)
            return self._fragments

    def _bindings(self, original):
        bindings = KeyBindings()

        @bindings.add("c-o")
        def toggle(event):
            with self._lock:
                if self.permission_pending:
                    return
                self.expanded = not self.expanded
                self.follow_tail = self.running
                if not self.running:
                    self.scroll_line = 0
                self._changed()

        @bindings.add("escape", filter=Condition(lambda: self.expanded and not self.permission_pending))
        def collapse(event):
            with self._lock:
                self.expanded = False
                self.follow_tail = self.running
                self.scroll_line = 0
                self._changed()

        @bindings.add("c-c", eager=True, filter=Condition(lambda: self.running))
        def interrupt(event):
            with self._lock:
                if self.permission_pending:
                    future = self._permission_future
                    self._clear_permission()
                    future.set_exception(KeyboardInterrupt())
                else:
                    self._interrupt()

        def scroll(amount):
            with self._lock:
                self.follow_tail = False
                self.scroll_line = max(0, min(self._line_count - 1, self.scroll_line + amount))
            self.session.app.invalidate()

        scrollable = Condition(lambda: self.expanded or self.permission_pending or not self.running)
        @bindings.add("pageup", filter=scrollable)
        def page_up(event):
            scroll(-max(1, self.window.render_info.window_height - 1) if self.window.render_info else -5)

        @bindings.add("pagedown", filter=scrollable)
        def page_down(event):
            scroll(max(1, self.window.render_info.window_height - 1) if self.window.render_info else 5)

        return merge_key_bindings([original or KeyBindings(), bindings])

    def _accept(self, buffer):
        with self._lock:
            if self.permission_pending:
                answer = buffer.text.strip()
                request = self._permission
                if answer in {"", "0"}:
                    decision = PermissionAnswer(False)
                elif answer == "1":
                    decision = PermissionAnswer(True)
                else:
                    try:
                        index = int(answer) - 2
                    except ValueError:
                        index = -1
                    if not 0 <= index < len(request.suggestions):
                        self.notice = "Please enter a listed number."
                        buffer.reset()
                        self._changed()
                        return True
                    decision = PermissionAnswer(True, (request.suggestions[index],))
                future = self._permission_future
                self._clear_permission()
                future.set_result(decision)
                return True
            if self.running:
                self.notice = "Task running. Your draft is kept; submit it after completion."
                self._changed()
                return True
            if not buffer.text.strip():
                buffer.reset()
                return True
        return self._original_accept(buffer)

    def _clear_permission(self):
        self._permission = None
        self._permission_future = None
        self.session.default_buffer.document = self._draft
        self.notice = ""
        self.follow_tail = self.running
        self._changed()

    def _run(self):
        session = self.session
        # Insert inside the existing completion FloatContainer, preserving all
        # native toolbars, menu geometry and the original input Window.
        container = self._container
        original_content = container.content
        saved = {name: getattr(session, name) for name in (
            "message", "multiline", "key_bindings", "completer", "refresh_interval")}
        original_erase = session.app.erase_when_done
        self._original_accept = session.default_buffer.accept_handler
        try:
            container.content = HSplit([self.window, original_content])
            session.default_buffer.accept_handler = self._accept
            session.app.erase_when_done = True
            result = session.prompt(
                lambda: [("class:prompt", "Choose (default: 0): " if self.permission_pending else "... " if self.multiline else "❯ ")],
                multiline=Condition(lambda: self.multiline and not self.permission_pending),
                key_bindings=self._bindings(saved["key_bindings"]),
                completer=DynamicCompleter(lambda: None if self.permission_pending else saved["completer"]),
                refresh_interval=0.1, pre_run=self._ready.set,
                handle_sigint=False, set_exception_handler=False,
            )
            self._next_input.set_result(result)
        except BaseException as exc:
            self.failure = exc if not isinstance(exc, (EOFError, KeyboardInterrupt)) else None
            self._next_input.set_exception(exc)
        finally:
            container.content = original_content
            session.default_buffer.accept_handler = self._original_accept
            session.app.erase_when_done = original_erase
            for name, value in saved.items():
                setattr(session, name, value)
            with self._lock:
                self._closed = True
                if self._permission_future is not None and not self._permission_future.done():
                    self._permission_future.set_exception(EOFError())
                    self._permission = None
                should_interrupt = self.running and self._ready.is_set() and not self._closing
            self._ready.set()
            if should_interrupt:
                self._interrupt()

    def start(self):
        self.thread.start()
        self._ready.wait()
        if self._closed:
            self._next_input.result()

    def ask_permission(self, request):
        future = Future()
        def show():
            with self._lock:
                if future.done() or self._closed:
                    return
                self._draft = self.session.default_buffer.document
                self.session.default_buffer.reset()
                self._permission = request
                self._permission_future = future
                self.notice = ""
                self.follow_tail = False
                self.scroll_line = 0
                self._changed()
        with self._lock:
            if self._closed:
                raise EOFError()
            self._permission_future = future
            self.session.app.loop.call_soon_threadsafe(show)
        try:
            return future.result()
        finally:
            future.cancel()
            def clear():
                with self._lock:
                    if self._permission_future is future and self.permission_pending:
                        self._clear_permission()
            with self._lock:
                if not self._closed:
                    self.session.app.loop.call_soon_threadsafe(clear)

    def read_next(self):
        try:
            return self._next_input.result()
        finally:
            self.thread.join()

    def close(self):
        with self._lock:
            self._closing = True
            if not self._closed and self.session.app.loop is not None:
                def exit_app():
                    if self.session.app.is_running:
                        self.session.app.exit(exception=EOFError())
                self.session.app.loop.call_soon_threadsafe(exit_app)
        if self.thread.is_alive():
            self.thread.join()
