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

from biopaster.agent.agent_loop import ToolEvent, agent_loop
from biopaster.agent.conversation import Conversation
from biopaster.config import get_provider_config
from biopaster.providers import get_provider_class
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.defaults import build_default_registry


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

        return False

    def _ask_permission(self, message: str) -> bool:
        if self._current_status is not None:
            try:
                self._current_status.stop()
            except Exception:
                pass

        self.console.print()
        self.console.print(
            "[bold yellow]Permission required[/bold yellow]"
        )
        self.console.print(message)

        answer = self.prompt_session.prompt(
            "Allow? [y/N] "
        ).strip().lower()

        return answer in {"y", "yes"}

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
            _stop_status_once()

            # A tool line must not be inserted into a Markdown block.
            renderer.finish()

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
                    max_turns=20,
                    stream=self.stream,
                    on_text_chunk=on_text_chunk,
                    on_event=on_event,
                )
        except Exception as e:
            renderer.finish()
            self.console.print(f"[red]Error: {e}[/red]")
            return
        finally:
            self._current_status = None

        renderer.finish()

        if not self.stream:
            self.console.print(Markdown(result.response_text))

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

