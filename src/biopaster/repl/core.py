from __future__ import annotations

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from biopaster.agent.agent_loop import ToolEvent, ResultEvent, agent_loop
from biopaster.agent.conversation import Conversation
from biopaster.config import get_provider_config
from biopaster.providers import get_provider_class
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.defaults import build_default_registry

from biopaster.agent.session_log import SessionLog
from biopaster.tool_system.permissions import (
    PermissionRequest,
    PermissionAnswer,
)


def build_prompt_session(commands: list[str]) -> PromptSession:
    command_completer = WordCompleter(
        commands,
        ignore_case=True,
        match_middle=True,
    )

    return PromptSession(
        history=DummyHistory(),
        completer=FuzzyCompleter(command_completer),
        complete_while_typing=True,
        style=Style.from_dict({
            "prompt": "bold cyan",
        }),
    )


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

        self.commands = [
            "/help",
            "/multiline",
            "/permissions",
            "/exit",
            "/quit",
        ]
        self.prompt_session = build_prompt_session(self.commands)

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
        
        self.session_log = SessionLog(Path.home() / ".biopaster" / "sessions")
        self.provider.session_log = self.session_log

        self.tool_registry = build_default_registry()
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

    def _handle_command(self, command: str) -> bool:
        if command == "/help":
            self.console.print(Markdown(
                """
    ## Commands

    - `/help` — Show available commands
    - `/multiline` — Toggle multiline input mode
    - `/exit` — Exit BioPaster
    - `/quit` — Exit BioPaster
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
                
        return False

    def _ask_permission(
        self,
        request: PermissionRequest,
    ) -> PermissionAnswer:
        if self._current_status is not None:
            self._current_status.stop()

        self.console.print()
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
        
    def _show_permissions(self) -> None:
        permissions = self.tool_context.permission_context

        while True:
            rules = list(permissions.session_rules)

            self.console.print(
                Text("Session permission rules", style="bold cyan")
            )

            if not rules:
                self.console.print("No session permission rules.")
                return

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
                "Enter a number to remove one rule, "
                "'all' to remove all rules, or press Enter to return."
            )

            try:
                answer = self.prompt_session.prompt("Remove: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                self.console.print()
                return

            if not answer:
                return

            if answer == "all":
                permissions.clear_session_rules()
                self.console.print(
                    Text(
                        "All session rules removed, including "
                        "allow, ask, and deny rules.",
                        style="yellow",
                    )
                )
                return

            try:
                index = int(answer)
            except ValueError:
                self.console.print("Please enter a listed number or 'all'.")
                continue

            if not 1 <= index <= len(rules):
                self.console.print("Number out of range.")
                continue

            permissions.remove_rule(rules[index - 1])

            self.console.print(
                Text("Selected permission rule removed.", style="yellow")
            )

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

    def chat(self, user_input: str) -> None:
        self.conversation.add_user_message(user_input)

        renderer = StreamingMarkdownRenderer(
            console=self.console,
            refresh_per_second=10,
        )
        visible_output_started = False

        def _stop_status_once() -> None:
            nonlocal visible_output_started

            if visible_output_started:
                return

            visible_output_started = True
            if self._current_status is not None:
                try:
                    self._current_status.stop()
                except Exception:
                    pass

        def on_text_chunk(chunk: str) -> None:
            if not chunk:
                return

            _stop_status_once()
            renderer.append(chunk)

        def on_event(event: ToolEvent) -> None:
            self.session_log.record("agent_event", event)
            _stop_status_once()
            # A tool line must not be inserted into a Markdown block.
            renderer.finish()
            
            if isinstance(event, ResultEvent):
                if event.is_error:
                    for error in event.errors:
                        self.console.print(
                            Text(error, style="red")
                        )
                elif not self.stream and event.result:
                    self.console.print(Markdown(event.result))
                return

            if event.kind == "tool_use":
                self.console.print(
                    f"[dim]•[/dim] "
                    f"[cyan]{event.tool_name}[/cyan] "
                    "[dim]running...[/dim]"
                )
                return

            if event.kind == "tool_result":
                if event.is_error:
                    self.console.print(
                        f"[red]  ↳ {event.tool_name} failed[/red]"
                    )
                    self.console.print(event.tool_output, markup=False)
                else:
                    self.console.print(
                        f"[dim]  ↳ {event.tool_name} completed[/dim]"
                    )
                return

            if event.kind == "tool_error":
                self.console.print(
                    f"[red]  ↳ {event.error or 'Error'}[/red]"
                )

        self.console.print("\n[bold cyan]BioPaster :[/bold cyan]")
        self._current_status = self.console.status(
            "[dim]Thinking...[/dim]",
            spinner="dots3",
            spinner_style="bright_cyan",
        )

        try:
            with self._current_status:
                result = agent_loop(
                    conversation=self.conversation,
                    provider=self.provider,
                    tool_registry=self.tool_registry,
                    tool_context=self.tool_context,
                    max_turns=500,
                    stream=self.stream,
                    on_text_chunk=on_text_chunk,
                    on_event=on_event,
                )
        except (KeyboardInterrupt, EOFError) as e:
            self.session_log.record_exception("run_interrupted", e)
            self.session_log.end_run("interrupted")
            self.console.print("\n[yellow]Interrupted.[/yellow]")
            return
        except Exception as e:
            self.session_log.record_exception("run_exception", e)
            self.session_log.end_run("failed")
            self.console.print(
                Text(f"Error: {e}", style="red")
            )
            return
        else:
            self.session_log.record("agent_return", result)
            self.session_log.end_run("returned")
        finally:
            renderer.finish()
            self._current_status = None
            
        self.console.print()

    def run(self) -> None:
        self._print_startup_header()

        while True:
            try:
                prompt_text = (
                    "... "
                    if self.multiline_mode
                    else "❯ "
                )
                user_input = self.prompt_session.prompt(
                    prompt_text,
                    multiline=self.multiline_mode,
                )
            except KeyboardInterrupt:
                self.console.print(
                    "\n[yellow]Interrupted. "
                    "Type /exit or /quit to quit.[/yellow]"
                )
                self.multiline_mode = False
                continue
            except EOFError:
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
