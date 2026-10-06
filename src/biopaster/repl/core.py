from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone

from biopaster.agent.session import (
    SessionSnapshot,
    save_session,
    list_sessions,
    load_session,
)
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from biopaster.agent.agent_loop import ToolEvent, ResultEvent, agent_loop
from biopaster.agent.conversation import Conversation, TextContentBlock
from biopaster.config import (
    get_configured_notebook_kernels,
    get_provider_config,
    load_config,
)
from biopaster.mcp.config import parse_mcp_servers
from biopaster.mcp.manager import open_mcp_tools
from biopaster.tool_system.registry import ToolRegistry
from biopaster.providers import get_provider_class
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.defaults import build_default_registry

from biopaster.agent.session_log import SessionLog
from biopaster.agent.session_title import generate_session_title
from .execution_view import ExecutionView
from biopaster.tool_system.permissions import (
    PermissionRequest,
    PermissionAnswer,
)

import json
from biopaster.agent.prompts import PERMISSION_REVIEW_SYSTEM_PROMPT
from biopaster.tool_system.permission_reviewer import review_permission
from biopaster.skills.loader import discover_skills
from biopaster.tasks.store import TaskStore



def select_saved_session(sessions: list[dict], prompt_session) -> str | None:
    """Select a session without adding a prompt to conversation/history."""
    if not sessions:
        return None
    selected = 0
    bindings = KeyBindings()

    @bindings.add("up")
    def previous(event):
        nonlocal selected
        selected = max(0, selected - 1)

    @bindings.add("down")
    def next_item(event):
        nonlocal selected
        selected = min(len(sessions) - 1, selected + 1)

    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=sessions[selected]["session_id"])

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    def render():
        fragments = []
        for index, item in enumerate(sessions):
            if index:
                fragments.append(("", "\n"))
            updated = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).astimezone()
            title = item.get('title') or item.get('preview') or '(No user text)'
            title = " ".join(title.split())
            title = "".join(char for char in title if char.isprintable())
            label = f"{updated:%m-%d %H:%M}  {title}  · {item['message_count']} messages"
            fragments.append(("reverse" if index == selected else "", ("❯ " if index == selected else "  ") + label))
        return fragments

    control = FormattedTextControl(render, focusable=True,
                                   get_cursor_position=lambda: Point(0, selected))
    window = Window(control, height=min(10, len(sessions)), wrap_lines=False,
                    right_margins=[ScrollbarMargin()], always_hide_cursor=True)
    app = Application(
        layout=Layout(HSplit([
            Window(FormattedTextControl("Resume session · ↑↓ select · Enter resume · Esc cancel"), height=1),
            window,
        ]), focused_element=control),
        key_bindings=bindings,
        input=prompt_session.app.input,
        output=prompt_session.app.output,
        erase_when_done=True,
        full_screen=False,
    )
    return app.run()


def build_prompt_session(commands: list[str]) -> PromptSession:
    command_completer = WordCompleter(
        commands,
        ignore_case=True,
        match_middle=True,
    )
    bindings = KeyBindings()

    @bindings.add(
        "enter",
        eager=True,
        filter=has_completions & Condition(
            lambda: not bool(session.multiline)
            and get_app().current_buffer.text.startswith("/")
        ),
    )
    def accept_command_completion(event):
        buffer = event.current_buffer
        if buffer.complete_state.current_completion is None:
            buffer.go_to_completion(0)
        buffer.validate_and_handle()

    session = PromptSession(
        history=InMemoryHistory(),
        completer=FuzzyCompleter(command_completer),
        complete_while_typing=True,
        key_bindings=bindings,
        style=Style.from_dict({
            "prompt": "bold cyan",
            "scrollbar.background": "bg:#333333",
            "scrollbar.button": "bg:#6B9AC4",
            "execution.border": "#6B8299",
        }),
    )
    return session


class StreamingMarkdownRenderer:
    """Render one growing Markdown segment in place with Rich Live."""

    def __init__(
        self,
        console: Console,
        refresh_per_second: int = 10,
    ) -> None:
        if refresh_per_second < 1:
            raise ValueError("refresh_per_second must be at least 1")

        self.console = console
        self.refresh_per_second = refresh_per_second
        self.buffer = ""
        self.live: Live | None = None
        
    @property
    def started(self) -> bool:
        return self.live is not None

    def append(self, chunk: str) -> None:
        if not chunk:
            return

        self.buffer += chunk

        if self.live is None:
            self.live = Live(
                console=self.console,
                get_renderable=lambda: Markdown(self.buffer),
                refresh_per_second=self.refresh_per_second,
                transient=False,
            )
            self.live.start()
        
    def finish(self) -> None:
        if self.live is None:
            self.buffer = ""
            return

        self.live.update(
            Markdown(self.buffer),
            refresh=True,
        )
        self.live.stop()
        self.live = None
        self.buffer = ""


class BioPasterStreamingREPL:
    def __init__(self, provider_name: str = "qwen") -> None:
        self.console = Console()
        self.provider_name = provider_name
        self.stream = True
        self.multiline_mode = False
        self._current_status = None
        self._execution_view = None

        self.commands = [
            "/help",
            "/multiline",
            "/permissions",
            "/resume",
            "/new",
            "/exit",
            "/quit",
        ]
        self.prompt_session = build_prompt_session(self.commands)
        self.session_log = SessionLog(Path.home() / ".biopaster" / "sessions")
        self.session_created_at = datetime.now(timezone.utc).isoformat()
        self.session_title = None
        self.title_attempted = False

        config = get_provider_config(provider_name)
        provider_class = get_provider_class(provider_name)

        self.conversation = Conversation()
        self.provider = provider_class(
            api_key=config["api_key"],
            base_url=config.get("base_url"),
            model=config.get("default_model"),
            context_window=config.get("context_window", 128_000),
            max_output_tokens=config.get("max_output_tokens"),
        )
        
        self.provider.session_log = self.session_log

        self.notebook_kernels = get_configured_notebook_kernels()
        self.tool_registry = ToolRegistry()
        self.tool_context = ToolContext(
            workspace_root=Path("/home/yan/test/BioPaster"),
            tools=self.tool_registry.list_tools(),
            notebook_path=Path(
                "/home/yan/test/BioPaster/notebook.ipynb"
            ),
        )
        self.tool_context.permission_context.permission_handler = (
            self._ask_permission
        )
        self.tool_context.session_log = self.session_log
        self.tool_context.permission_context.session_log = self.session_log
        self.tool_context.permission_context.review_handler = (
            self._review_permission
        )
        
        task_store = TaskStore(
            Path.home()
            / ".biopaster"
            / "tasks"
            / self.session_log.session_id
            / "tasks.json"
        )

        task_store.initialize()
        self.tool_context.task_store = task_store
                

    def _handle_command(self, command: str) -> bool:
        if command == "/help":
            self.console.print(Markdown(
                """
    ## Commands

    - `/help` — Show available commands
    - `/multiline` — Toggle multiline input mode
    - `/resume` — Select and resume a saved session
    - `/new` — Save the current conversation and start a new one
    - `/exit` — Exit BioPaster
    - `/quit` — Exit BioPaster
    - `Ctrl+O` — Expand/collapse the current request's execution details
    - `PgUp` / `PgDn` — Scroll execution details or the final answer
    """
            ))
            return True

        if command == "/multiline":
            self.multiline_mode = not self.multiline_mode
            status = "enabled" if self.multiline_mode else "disabled"

            self.console.print(
                f"[green]Multiline mode {status}.[/green]"
            )

            if self.multiline_mode:
                self.console.print(
                    "[dim]Use Esc+Enter to submit multiline input.[/dim]"
                )
            return True
        
        if command == "/permissions":
            self._show_permissions()
            return True
        
        parts = command.split()

        if parts and parts[0] == "/new":
            if len(parts) == 1:
                self._new_session()
            else:
                self.console.print("Usage: /new", style="yellow")
            return True

        if parts and parts[0] == "/resume":
            if len(parts) == 1:
                self._show_sessions()
            else:
                self.console.print(
                    "Usage: /resume (select a session with the arrow keys)",
                    style="yellow",
                )
            return True
                
        return False
    
    def _new_session(self) -> None:
        """Start an independent conversation without deleting old artifacts."""
        if self._execution_view is not None and self._execution_view.running:
            self.console.print("Cannot start a new session while a request is running.", style="yellow")
            return

        try:
            if self.conversation.messages:
                self._save_session_snapshot()

            old_context = self.tool_context
            new_context = ToolContext(
                workspace_root=old_context.workspace_root,
                cwd=old_context.cwd,
                notebook_path=old_context.notebook_path,
                skills=dict(old_context.skills),
                tools=self.tool_registry.list_tools(),
                mcp_clients=dict(old_context.mcp_clients),
            )
            permissions = new_context.permission_context
            permissions.readonly_directories = old_context.permission_context.readonly_directories
            permissions.permission_handler = self._ask_permission
            permissions.review_handler = self._review_permission

            new_log = SessionLog(Path.home() / ".biopaster" / "sessions")
            new_store = TaskStore(
                Path.home() / ".biopaster" / "tasks" / new_log.session_id / "tasks.json"
            )
            new_store.initialize()
            new_context.task_store = new_store
            new_context.session_log = new_log
            permissions.session_log = new_log
        except Exception as exc:
            self.console.print(f"Unable to start a new session: {exc}", style="red", markup=False)
            return

        self._close_execution_view()
        self.conversation = Conversation()
        self.tool_context = new_context
        self.session_log = new_log
        self.provider.session_log = new_log
        self.session_created_at = datetime.now(timezone.utc).isoformat()
        self.session_title = None
        self.title_attempted = False
        self.multiline_mode = False
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[3J\033[H")
            sys.stdout.flush()
        self._print_startup_header()
        self.console.print("New conversation started. Existing files and notebook were kept.", style="dim")

    def _prepare_session_restore(
        self,
        session_id: str,
    ) -> tuple[SessionSnapshot, TaskStore, list[str]]:
        """Load and validate restore candidates without switching sessions."""
        snapshot = load_session(session_id)

        for label, path in (
            ("Workspace", snapshot.workspace_root),
            ("Working directory", snapshot.cwd),
        ):
            if not path.is_dir():
                raise FileNotFoundError(
                    f"{label} is unavailable or is not a directory: {path}"
                )

        task_path = (
            Path.home()
            / ".biopaster"
            / "tasks"
            / snapshot.task_list_id
            / "tasks.json"
        )

        task_store = TaskStore(task_path)

        # Validate the existing task file. Do not initialize a new one.
        task_store.list_tasks()

        repaired_ids = (
            snapshot.conversation.repair_pending_tool_results()
        )

        return snapshot, task_store, repaired_ids

    def _resume_session(self, session_id: str) -> None:
        """Restore conversation and tasks without replaying any operations."""
        view = self._execution_view
        if view is not None and view.running:
            self.console.print(
                "Cannot resume a session while a request is running.",
                style="yellow",
            )
            return

        if session_id == self.session_log.session_id:
            self.console.print("This session is already active.")
            return

        try:
            # Prepare the candidate without changing the active session.
            snapshot, task_store, repaired_ids = (
                self._prepare_session_restore(session_id)
            )

            user_skills_dir = (
                Path.home() / ".biopaster" / "skills"
            ).resolve()

            skills, diagnostics = discover_skills(
                workspace_root=snapshot.workspace_root,
                user_skills_dir=user_skills_dir,
            )

            new_context = ToolContext(
                workspace_root=snapshot.workspace_root,
                cwd=snapshot.cwd,
                notebook_path=snapshot.notebook_path,
                task_store=task_store,
                skills=skills,
                tools=self.tool_registry.list_tools(),
                mcp_clients=dict(self.tool_context.mcp_clients),
            )

            permissions = new_context.permission_context
            permissions.readonly_directories = (user_skills_dir,)
            permissions.permission_handler = self._ask_permission
            permissions.review_handler = self._review_permission

            # Do not abandon the current conversation if saving fails.
            if self.conversation.messages:
                self._save_session_snapshot()

            new_log = SessionLog.open_existing(
                Path.home() / ".biopaster" / "sessions",
                snapshot.session_id,
            )
            new_context.session_log = new_log
            permissions.session_log = new_log

        except Exception as exc:
            self.console.print(
                f"Unable to resume session: {type(exc).__name__}: {exc}",
                style="red",
                markup=False,
            )
            return

        # All preparation succeeded. Switch the active references.
        self._close_execution_view()

        self.conversation = snapshot.conversation
        self.session_created_at = snapshot.created_at
        self.session_title = snapshot.title
        self.title_attempted = snapshot.title_attempted
        self.tool_context = new_context
        self.session_log = new_log
        self.provider.session_log = new_log
        if sys.stdout.isatty():
            # Clear both the visible screen and terminal scrollback on resume.
            sys.stdout.write("\033[2J\033[3J\033[H")
            sys.stdout.flush()
        self._print_startup_header()

        try:
            new_log.record(
                "session_resumed",
                {
                    "repaired_tool_use_ids": repaired_ids,
                    "provider": self.provider_name,
                    "model": self.provider.model,
                },
            )
        except Exception as exc:
            self.console.print(
                f"Session resumed, but logging failed: {exc}",
                style="yellow",
                markup=False,
            )

        # self.console.print(
        #     f"Resumed session: {snapshot.session_id}",
        #     style="green",
        #     markup=False,
        # )
        # self.console.print(
        #     f"Workspace: {new_context.workspace_root}",
        #     markup=False,
        # )
        self.console.print(
            f"Messages: {len(self.conversation.messages)}"
        )
        self.console.print(
            "Kernel variables and temporary permissions were not restored.",
            style="yellow",
        )

        if repaired_ids:
            self.console.print(
                "Missing tool results were repaired. Verify execution state "
                "before retrying these calls: " + ", ".join(repaired_ids),
                style="yellow",
                markup=False,
            )

        if (
            snapshot.notebook_path is not None
            and not snapshot.notebook_path.exists()
        ):
            self.console.print(
                f"Previous notebook is missing: {snapshot.notebook_path}",
                style="yellow",
                markup=False,
            )

        if (
            snapshot.provider != self.provider_name
            or snapshot.model != self.provider.model
        ):
            self.console.print(
                f"Using current provider/model: "
                f"{self.provider_name}/{self.provider.model}; "
                f"saved session used {snapshot.provider}/{snapshot.model}.",
                style="yellow",
                markup=False,
            )

        for message in diagnostics:
            self.console.print(
                f"Skill discovery warning: {message}",
                style="yellow",
                markup=False,
            )
        self._show_conversation_history()
        
    def _show_conversation_history(self) -> None:
        """Display restored text without replaying tools or model requests."""
        self.console.print()
        self.console.rule("Restored conversation")

        for message in self.conversation.messages:
            if message._is_internal:
                continue

            if message.role not in {"user", "assistant"}:
                continue

            if isinstance(message.content, str):
                text = message.content
            else:
                text = "\n\n".join(
                    block.text
                    for block in message.content
                    if isinstance(block, TextContentBlock)
                )

            if not text.strip():
                continue

            if message.role == "user":
                self.console.print(
                    Text.assemble(
                        ("❯ ", "bold cyan"),
                        text,
                    )
                )
            else:
                self.console.print("BioPaster:", style="bold cyan")
                self.console.print(Markdown(text))

            self.console.print()

        self.console.rule("End of restored conversation")
        self.console.print()
            
    def _show_sessions(self) -> None:
        """Display saved session snapshots without loading them."""
        try:
            sessions, diagnostics = list_sessions()
        except OSError as exc:
            self.console.print(
                f"Unable to list sessions: {exc}",
                style="red",
                markup=False,
            )
            return

        if not sessions:
            self.console.print(
                "No recoverable session snapshots found.",
                style="yellow",
            )

        for message in diagnostics:
            self.console.print(
                f"Session warning: {message}",
                style="yellow",
                markup=False,
        )

        if sessions:
            sessions.sort(
                key=lambda item: datetime.fromisoformat(
                    item["updated_at"].replace("Z", "+00:00")
                ).timestamp(),
                reverse=True,
            )
            selected = select_saved_session(sessions, self.prompt_session)
            if selected is not None:
                self._resume_session(selected)

    def _ask_permission(
        self,
        request: PermissionRequest,
    ) -> PermissionAnswer:
        if self._execution_view is not None:
            return self._execution_view.ask_permission(request)
        answer = self._ask_permission_impl(request)
        if self._current_status is not None:
            self._current_status.start()
        return answer

    def _ask_permission_impl(
        self,
        request: PermissionRequest,
    ) -> PermissionAnswer:
        if self._current_status is not None:
            self._current_status.stop()

        self.console.print()

        if request.review_error is not None:
            self.console.print(
                "Automatic permission review failed. "
                "Falling back to manual approval.",
                style="yellow",
            )
            self.console.print(
                f"Reason: {request.review_error}",
                markup=False,
            )

        self.console.print(
            Text("Permission required", style="bold yellow")
        )
        
        self.console.print(
            f"Tool: {request.tool_name}",
            markup=False,
        )
        self.console.print(request.description, markup=False)

        self.console.print("\nTargets:")
        for target in request.targets:
            content = (
                target.rule_content
                if target.rule_content is not None
                else "entire tool"
            )
            self.console.print(
                f"- {target.tool_name}: {content}",
                markup=False,
            )

        self.console.print("\n1. Allow once")

        for index, rule in enumerate(request.suggestions, start=2):
            scope = (
                "entire tool"
                if rule.rule_content is None
                else repr(rule.rule_content)
            )
            self.console.print(
                f"{index}. Allow once and remember for this session: "
                f"{rule.tool_name} ({scope})",
                markup=False,
            )

        self.console.print("0. Deny")

        while True:
            answer = self.prompt_session.prompt(
                "Choose (default: 0): "
            ).strip()

            if answer in {"", "0"}:
                return PermissionAnswer(allowed=False)

            if answer == "1":
                return PermissionAnswer(allowed=True)

            try:
                index = int(answer) - 2
            except ValueError:
                self.console.print("Please enter a listed number.")
                continue

            if not 0 <= index < len(request.suggestions):
                self.console.print("Please enter a listed number.")
                continue

            return PermissionAnswer(
                allowed=True,
                rules=(request.suggestions[index],),
            )
            
    def _review_permission(self, request, decisions):
        environment = {
            "workspace_root": str(self.tool_context.workspace_root),
            "trusted_remote_repositories": [],
            "trusted_internal_domains": [],
            "trusted_cloud_buckets": [],
            "key_internal_services": [],
        }

        system_prompt = (
            PERMISSION_REVIEW_SYSTEM_PROMPT
            + "\n\n## Application-provided Runtime Environment\n"
            + json.dumps(environment, ensure_ascii=False)
        )

        return review_permission(
            provider=self.provider,
            messages=self.conversation.messages,
            request=request,
            decisions=decisions,
            system_prompt=system_prompt,
        )
        
    def _show_permissions(self) -> None:
        permissions = self.tool_context.permission_context

        while True:
            self.console.print(
                Text("Session permissions", style="bold cyan")
            )
            self.console.print(f"Current mode: {permissions.mode}")

            rules = list(permissions.session_rules)

            if not rules:
                self.console.print("No session permission rules.")

            for index, rule in enumerate(rules, start=1):
                scope = (
                    "entire tool"
                    if rule.rule_content is None
                    else repr(rule.rule_content)
                )
                self.console.print(
                    f"{index}. {rule.behavior.upper()} "
                    f"{rule.tool_name} ({scope}) "
                    f"[source: {rule.source}]",
                    markup=False,
                )

            self.console.print(
                "\nEnter 'default' or 'auto' to switch mode, "
                "a number to remove a rule, "
                "'all' to clear rules, or Enter to return."
            )

            try:
                answer = self.prompt_session.prompt(
                    "Permissions: "
                ).strip().lower()

                if answer == "auto" and permissions.mode != "auto":
                    if permissions.review_handler is None:
                        self.console.print(
                            "Auto mode is unavailable: no reviewer configured."
                        )
                        continue

                    self.console.print(
                        Text(
                            "Auto mode lets a model approve operations that "
                            "would otherwise require confirmation. "
                            "Model approval is not sandbox protection. "
                            "Explicit ask and deny rules remain effective.",
                            style="yellow",
                        )
                    )
                    confirmation = self.prompt_session.prompt(
                        "Enable auto mode? [y/N]: "
                    ).strip().lower()

                    if confirmation not in {"y", "yes"}:
                        continue

            except (KeyboardInterrupt, EOFError):
                self.console.print()
                return

            if not answer:
                return

            if answer in {"default", "auto"}:
                permissions.mode = answer
                self.console.print(f"Permission mode: {answer}")
                return

            if answer == "all":
                permissions.clear_session_rules()
                self.console.print(
                    "All session rules removed. "
                    f"Mode remains: {permissions.mode}"
                )
                return

            try:
                index = int(answer)
            except ValueError:
                self.console.print(
                    "Enter 'default', 'auto', 'all', or a listed number."
                )
                continue

            if not 1 <= index <= len(rules):
                self.console.print("Number out of range.")
                continue

            permissions.remove_rule(rules[index - 1])
            self.console.print("Selected permission rule removed.")

    def _print_startup_header(self) -> None:
        information = Table.grid(padding=(0, 1))
        information.add_row(
            "Model",
            Text(
                self.provider.model or "Unknown",
                style="bold magenta",
            ),
        )
        information.add_row(
            "Workspace",
            Text(
                str(self.tool_context.workspace_root),
                style="cyan",
            ),
        )

        self.console.print(
            Panel(
                information,
                title="[bold cyan] BioPaster [/bold cyan]",
                # subtitle="[dim]/multiline · /exit[/dim]",
                border_style="bright_black",
                padding=(1, 2),
            )
        )
        self.console.print()

    def _close_execution_view(self) -> None:
        view = self._execution_view
        if view is None:
            return
        view.close()
        self._execution_view = None
        self.console.print("\n[bold cyan]BioPaster :[/bold cyan]")
        if view.result:
            self.console.print(Markdown(view.result))
        for error in view.errors:
            self.console.print(Text(error, style="red"))
        self.console.print()

    def _read_input(self, prompt_text: str) -> str:
        if self._execution_view is None:
            return self.prompt_session.prompt(prompt_text, multiline=self.multiline_mode)
        user_input = self._execution_view.read_next()
        self._close_execution_view()
        self.console.print(Text.assemble((prompt_text, "bold cyan"), user_input))
        return user_input
    
    def chat(self, user_input: str) -> None:
        self._close_execution_view()
        self.conversation.add_user_message(user_input)
        self._autosave_session(interrupted=True)
        view = ExecutionView(
            self.prompt_session,
            multiline=self.multiline_mode,
            task_store=self.tool_context.task_store,
        )
        self._execution_view = view
        view.refresh_tasks()

        def on_event(event: ToolEvent | ResultEvent) -> None:
            self.session_log.record("agent_event", event)
            if isinstance(event, ResultEvent):
                view.finish(event.result, event.errors if event.is_error else [])
            else:
                view.tool_event(event)
                if (
                    event.kind in {"tool_result", "tool_error"}
                    and event.tool_name.casefold() in {
                        "taskcreate", "taskget", "taskupdate", "tasklist"
                    }
                ):
                    view.refresh_tasks()

        try:
            interrupted = True
            view.start()
            result = agent_loop(
                conversation=self.conversation,
                provider=self.provider,
                tool_registry=self.tool_registry,
                tool_context=self.tool_context,
                max_turns=500,
                stream=self.stream,
                on_text_chunk=view.append_text,
                on_event=on_event,
                on_checkpoint=lambda: self._autosave_session(interrupted=True),
            )
        except (KeyboardInterrupt, EOFError) as e:
            self.session_log.record_exception("run_interrupted", e)
            self.session_log.end_run("interrupted")
            view.finish(errors=[f"Display error: {view.failure}" if view.failure else "Interrupted."])
        except Exception as e:
            self.session_log.record_exception("run_exception", e)
            self.session_log.end_run("failed")
            view.finish(errors=[f"Error: {e}"])
        else:
            interrupted = False
            self.session_log.record("agent_return", result)
            self.session_log.end_run("returned")
            if view.running:
                view.finish(result.response_text)
        finally:
            self._autosave_session(interrupted=interrupted)

        if not interrupted and result.response_text.strip() and not self.title_attempted:
            self.title_attempted = True
            # Persist the attempt before requesting a title; never lose the answer.
            self._autosave_session()
            try:
                self.session_title = generate_session_title(
                    self.provider, user_input, result.response_text
                )
            except (Exception, KeyboardInterrupt, EOFError) as exc:
                try:
                    self.session_log.record_exception("session_title_failed", exc)
                except Exception:
                    pass
            finally:
                self._autosave_session()

    def run(self) -> None:
        self._print_startup_header()
        user_skills_dir = (
            Path.home() / ".biopaster" / "skills"
        ).resolve()

        skills, diagnostics = discover_skills(
            workspace_root=self.tool_context.workspace_root,
            user_skills_dir=user_skills_dir,
        )
        self.tool_context.skills = skills

        permissions = self.tool_context.permission_context
        permissions.readonly_directories = tuple(dict.fromkeys(
            (*permissions.readonly_directories, user_skills_dir)
        ))

        for message in diagnostics:
            self.console.print(
                f"Skill discovery warning: {message}",
                style="yellow",
                markup=False,
            )

        try:
            app_config = load_config()
            mcp_config = parse_mcp_servers(app_config)
            
            for server_name, error_message in mcp_config.errors.items():
                self.console.print(
                    f"MCP config error for '{server_name}': {error_message}",
                    style="yellow",
                    markup=False,
                )
                
            server_configs = mcp_config.servers

            with open_mcp_tools(server_configs) as mcp_startup:
                self.tool_registry = build_default_registry(
                    self.notebook_kernels,
                    extra_tools=mcp_startup.tools.values(),
                )

                self.tool_context.tools = self.tool_registry.list_tools()

                self.tool_context.mcp_clients = {
                    server_name: {"connected": True}
                    for server_name in mcp_startup.connected_servers
                }

                for server_name, error_type in mcp_startup.errors.items():
                    self.console.print(
                        f"MCP server '{server_name}' unavailable: {error_type}",
                        style="yellow",
                        markup=False,
                    )

                self.console.print(
                    f"MCP tools loaded: {len(mcp_startup.tools)}"
                )

                self._run_loop()

        finally:
            self.tool_registry = ToolRegistry()
            self.tool_context.tools = []
            self.tool_context.mcp_clients = {}
            self._close_execution_view()

    def _run_loop(self) -> None:
        while True:
            try:
                prompt_text = (
                    "... "
                    if self.multiline_mode
                    else "❯ "
                )
                user_input = self._read_input(prompt_text)
            except KeyboardInterrupt:
                self._close_execution_view()
                self.console.print(
                    "\n[yellow]Interrupted. "
                    "Type /exit or /quit to quit.[/yellow]"
                )
                self.multiline_mode = False
                continue
            except EOFError:
                self._close_execution_view()
                self.console.print("\n[blue]Goodbye![/blue]")
                break

            user_input = user_input.strip()
            command = user_input.lower()
            if command in {"/exit", "/quit"}:
                self.console.print("[blue]Goodbye![/blue]")
                break
            
            if command.startswith("/"):
                if not self._handle_command(command):
                    self.console.print(
                        f"[red]Unknown command: {user_input}[/red]"
                    )
                continue

            self.chat(user_input)
            self.multiline_mode = False


    def _save_session_snapshot(self, *, interrupted: bool = False) -> None:
        """Build and save a snapshot of the current logical session."""
        context = self.tool_context
        session_id = self.session_log.session_id

        if context.task_store is None:
            raise RuntimeError("Task storage is not configured.")

        expected_task_path = (
            Path.home()
            / ".biopaster"
            / "tasks"
            / session_id
            / "tasks.json"
        ).resolve()

        if context.task_store.path != expected_task_path:
            raise RuntimeError(
                "Task storage does not belong to the current session."
            )

        snapshot = SessionSnapshot(
            session_id=session_id,
            created_at=self.session_created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            provider=self.provider_name,
            model=self.provider.model,
            workspace_root=context.workspace_root,
            cwd=context.cwd,
            notebook_path=context.notebook_path,
            task_list_id=session_id,
            conversation=self.conversation,
            interrupted=interrupted,
            title=self.session_title,
            title_attempted=self.title_attempted,
        )

        save_session(snapshot)
        
        
    def _autosave_session(self, *, interrupted: bool = False) -> None:
        """Save without turning a persistence error into an agent failure."""
        try:
            self._save_session_snapshot(interrupted=interrupted)
        except Exception as e:
            message = (
                f"Session autosave failed: {type(e).__name__}: {e}. "
                "Recent conversation changes may not be recoverable."
            )

            # A logging failure must not hide the original save failure.
            try:
                self.session_log.record_exception(
                    "session_autosave_failed", e
                )
            except Exception:
                pass

            view = self._execution_view
            if view is not None and view.session.app.is_running:
                with view._lock:
                    view.notice = message
                    view._changed()
            else:
                self.console.print(
                    message,
                    style="yellow",
                    markup=False,
                )
