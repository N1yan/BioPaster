"""Fold one request's progress above the existing PromptSession input."""

from __future__ import annotations

from concurrent.futures import Future
from io import StringIO
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
from prompt_toolkit.layout import ConditionalContainer, FloatContainer, HSplit, Window
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from biopaster.agent.agent_loop import ToolEvent
from biopaster.tool_system.permissions import PermissionAnswer


def format_permission_description(description: str) -> Text:
    content_fields = {"Command", "Code", "Content", "Old text", "New text", "Markdown", "Arguments"}
    info_fields = {
        "Working directory", "Kernel", "Language", "Environment",
        "Notebook", "Permission analysis", "Replace all",
    }
    text = Text()
    content_style = None
    for line in description.splitlines(keepends=True):
        label, separator, value = line.partition(":")
        if separator and label in content_fields | info_fields:
            content_style = "bold bright_blue" if label in content_fields else None
            text.append(label + separator, style="dim")
            text.append(value, style=content_style)
        else:
            text.append(line, style=content_style)
    return text


def is_displayed_bash_command(request, tool_name, scope):
    """Recognize the exact command already included by Bash's description."""
    return (
        request.tool_name.casefold() == tool_name.casefold() == "bash"
        and bool(scope)
        and any(rule.tool_name.casefold() == "bash" and rule.rule_content == scope
                for rule in request.suggestions)
        and f"Command:\n{scope}" in request.description
    )


class ExecutionView:
    """Keep native editing/completion and add only a render region and bindings.

    The agent stays on the main thread, preserving its KeyboardInterrupt path.
    This thread runs the SAME PromptSession, including its original buffer,
    completion menu, key bindings, history and input processors. All temporary
    changes are restored when the next question is submitted or the view closes.
    """

    def __init__(self, session, *, multiline=False, interrupt=None, task_store=None,
                 render_history=None):
        self.session = session
        self.render_history = render_history
        self._terminal_size = None
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
        self.task_store = task_store
        self._task_lines = []  # Display snapshot, never an editable task store.
        self.tasks_expanded = False
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
        self.header_window = Window(
            FormattedTextControl(self._render_header, focusable=False),
            height=1,
            dont_extend_height=True,
            always_hide_cursor=True,
        )
        self.window = Window(self.control, wrap_lines=False, always_hide_cursor=True,
                             height=self._height, dont_extend_height=True,
                             right_margins=[ScrollbarMargin()])
        self.footer_window = Window(
            FormattedTextControl(self._render_footer, focusable=False),
            height=1,
            dont_extend_height=True,
            always_hide_cursor=True,
        )
        self.tasks_window = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_tasks, focusable=False),
                height=lambda: len(self._visible_task_lines()),
                wrap_lines=False,
                dont_extend_height=True,
                always_hide_cursor=True,
            ),
            filter=Condition(lambda: bool(self._task_lines)),
        )

    def refresh_tasks(self):
        """Refresh from storage on events, not on every rendering frame."""
        lines = []
        if self.task_store is not None:
            try:
                tasks = self.task_store.list_tasks()
                if tasks:
                    by_id = {task.id: task for task in tasks}
                    done = sum(task.status == "completed" for task in tasks)
                    title = f"Tasks · {done}/{len(tasks)} completed"
                    if done == len(tasks):
                        title += " · All completed"
                    lines.append(("bold ansicyan", title))
                    for task in sorted(tasks, key=lambda task: int(task.id)):
                        icon, style = {
                            "pending": ("○", ""),
                            "in_progress": ("→", "ansicyan"),
                            "completed": ("✓", "ansigreen"),
                        }[task.status]
                        subject = " ".join(task.subject.split())
                        subject = "".join(c for c in subject if c.isprintable())
                        text = f"  {icon} #{task.id} {subject}"
                        blockers = [key for key in task.blockedBy
                                    if key not in by_id or by_id[key].status != "completed"]
                        if blockers and task.status != "completed":
                            text += " · Waiting for " + ", ".join(f"#{key}" for key in blockers)
                        lines.append((style, text))
            except Exception:
                lines = [("ansiyellow", "Tasks unavailable: could not read task storage.")]
        with self._lock:
            self._task_lines = lines
            self._changed()

    def _visible_task_lines(self):
        with self._lock:
            if not self._task_lines or self.permission_pending:
                return []
            style, title = self._task_lines[0]
            if self.tasks_expanded:
                return [(style, "▼ " + title + " · Ctrl+T collapse"),
                        *self._task_lines[1:]]
            return [(style, "▶ " + title + " · Ctrl+T expand")]

    def _render_tasks(self):
        fragments = []
        for index, (style, text) in enumerate(self._visible_task_lines()):
            if index:
                fragments.append(("", "\n"))
            fragments.append((style, text))
        return fragments

    @property
    def permission_pending(self):
        return self._permission is not None

    @property
    def can_scroll(self):
        return self.expanded or self.permission_pending or not self.running

    def _height(self):
        rows = self.session.app.output.get_size().rows
        # Header and footer each use one row. Keep the total execution region
        # within the previous height budget so the input remains visible.
        budget = rows - 7 if not self.running else rows // 2 - 2
        return Dimension(min=1, max=max(1, budget - len(self._visible_task_lines())))

    def _separator(self, label):
        width = max(1, self.session.app.output.get_size().columns - 1)
        prefix = "── "
        text = prefix + label + " "
        return text[:width] + "─" * max(0, width - len(text))

    def _render_header(self):
        return [("class:execution.border", self._separator("Current execution"))]

    def _render_footer(self):
        with self._lock:
            if self.permission_pending:
                label = "Permission required"
            elif self.expanded:
                label = "▼ Execution details · Ctrl+O to collapse"
            else:
                label = "▶ Execution details · Ctrl+O to expand"
            if self.can_scroll:
                label += " · PgUp/PgDn to scroll"
            return [("class:execution.border", self._separator(label))]

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

    def _permission_lines(self):
        request = self._permission
        lines = [
            Text("Permission required", style="bold cyan"),
            Text.assemble(("Tool: ", "dim"), (request.tool_name, "bold cyan")),
            format_permission_description(request.description),
        ]
        if request.review_error:
            lines += [
                Text("Automatic permission review failed.", style="magenta"),
                Text(request.review_error, style="red"),
            ]
        lines.append(Text.assemble(("1. ", "bold cyan"), ("Allow once", "green")))
        for index, rule in enumerate(request.suggestions, 2):
            scope = rule.rule_content if rule.rule_content is not None else "entire tool"
            style = "bold bright_blue" if rule.rule_content is None else "bright_blue"
            label = (
                "Allow once and remember this command for this session"
                if is_displayed_bash_command(request, rule.tool_name, rule.rule_content)
                else f"Allow once and remember for this session: {rule.tool_name} ({scope})"
            )
            lines.append(Text.assemble(
                (f"{index}. ", "bold cyan"),
                (label, style),
            ))
        lines.append(Text.assemble(("0. ", "bold cyan"), ("Deny", "red")))
        return Text("\n").join(lines)

    def _render(self):
        with self._lock:
            width = max(1, self.session.app.output.get_size().columns - 1)
            frame = int(time.monotonic() * 10) % 10 if self.running and not self.permission_pending else -1
            key = (self._revision, self.expanded, width, frame)
            if key != self._render_key:
                parts = [Text("BioPaster :", style="bold cyan")]
                if self.permission_pending:
                    parts.append(self._permission_lines())
                else:
                    entries = self.entries if self.expanded else self.entries[-4:] if self.running else []
                    for entry in entries:
                        if entry["kind"] == "text":
                            parts.append(Markdown(entry["text"], hyperlinks=False))
                        else:
                            parts.append(Text(f"{entry['name']} · {entry['status']}", style="dim"))
                    if self.running:
                        parts.append(Text(f"{'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[frame]} {self.status}", style="cyan"))
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
                self.tasks_expanded = False
                self.follow_tail = self.running
                if not self.running:
                    self.scroll_line = 0
                self._changed()

        @bindings.add("c-t", filter=Condition(lambda: bool(self._task_lines) and not self.permission_pending))
        def toggle_tasks(event):
            with self._lock:
                self.tasks_expanded = not self.tasks_expanded
                if self.tasks_expanded:
                    self.expanded = False
                self._changed()

        @bindings.add("escape", filter=Condition(lambda: (self.expanded or self.tasks_expanded) and not self.permission_pending))
        def collapse(event):
            with self._lock:
                self.expanded = False
                self.tasks_expanded = False
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

        scrollable = Condition(lambda: self.can_scroll)
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

    def _before_render(self, app):
        size = app.output.get_size()
        previous = self._terminal_size
        self._terminal_size = size
        if previous is None or size == previous or app.is_done or self.render_history is None:
            return

        # Reflow can push parts of the old live area into terminal scrollback,
        # where cursor-up/erase-down cannot reach them. Rebuild the visible
        # conversation before prompt_toolkit paints the current live area.
        history = self.render_history(max(1, size.columns - 1))
        app.renderer.reset()
        app.output.erase_screen()
        app.output.write_raw("\033[3J")
        app.output.cursor_goto(0, 0)
        app.output.write_raw(history.replace("\n", "\r\n"))
        app.output.flush()
        app.renderer.request_absolute_cursor_position()

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
            self._terminal_size = session.app.output.get_size()
            session.app.before_render += self._before_render
            container.content = HSplit([
                self.header_window,
                self.window,
                self.footer_window,
                self.tasks_window,
                original_content,
            ])
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
            session.app.before_render -= self._before_render
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
