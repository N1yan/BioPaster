from typing import Any, Mapping, Iterable, Protocol
from .protocol import ToolResult, ToolCall
from .context import ToolContext
from dataclasses import dataclass, replace
from .errors import ToolInputError, ToolPermissionError
from .permissions import PermissionRequest, PermissionTarget

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    aliases: tuple[str, ...] = ()
    is_read_only: bool = False
    is_destructive: bool = False
    strict: bool = False
    max_result_size_chars: int = 20_000


class Tool(Protocol):
    def spec(self) -> ToolSpec: ...

    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]: ...

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest: ...

    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...
    
class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: list[Tool] = []
        self._by_name: dict[str, Tool] = {}
        if tools:
            for tool in tools:
                self.register(tool)
    
    def register(self, tool: Tool) -> None:
        spec = tool.spec()
        key = spec.name.lower()
        if key in self._by_name:
            raise ValueError(f"Tool with name {spec.name} already registered")
        self._tools.append(tool)
        self._by_name[key] = tool
        for alias in spec.aliases:
            alias_key = alias.lower()
            if alias_key in self._by_name:
                raise ValueError(f"duplicate tool alias: {alias}")
            self._by_name[alias_key] = tool
            
    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools]
    
    def list_tools(self) -> list[str]:
        return list(self._by_name.keys())
    
    
    def dispatch(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool_name = call.name

        def error_result(message: str) -> ToolResult:
            return ToolResult(
                name=tool_name,
                output=[{"type": "text", "content": message, "metadata": {}}],
                is_error=True,
                tool_use_id=call.tool_use_id,
            )

        tool = self._by_name.get(tool_name.lower())
        if tool is None:
            return error_result(f"Tool {tool_name} not found.")

        tool_name = tool.spec().name
        previous_tool_use_id = context.active_tool_use_id
        try:
            context.active_tool_use_id = call.tool_use_id
            if not isinstance(call.input, dict):
                raise ToolInputError("Tool input must be a dictionary.")

            if not callable(getattr(tool, "prepare_input", None)):
                raise ToolInputError(
                    f"{tool_name} has not implemented prepare_input."
                )

            if not callable(getattr(tool, "check_permissions", None)):
                raise ToolInputError(
                    f"{tool_name} has not implemented check_permissions."
                )

            # Reject whole-tool bans before preparing input.
            decision = context.permission_context.evaluate(
                PermissionTarget(
                    tool_name=tool_name,
                    rule_content=None,
                )
            )
            if decision.behavior == "deny":
                context.permission_context.record_permission_decision(
                    tool_name=tool_name,
                    tool_use_id=call.tool_use_id,
                    outcome="denied",
                    reason=decision.reason,
                )
                raise ToolPermissionError(decision.reason)

            prepared_input = tool.prepare_input(call.input, context)
            request = tool.check_permissions(prepared_input, context)

            targets = request.targets

            if (
                decision.behavior == "ask"
                and decision.matched_rule is not None
            ):
                tool_target = PermissionTarget(
                    tool_name=tool_name,
                    rule_content=None,
                )
                if tool_target not in targets:
                    targets = (tool_target, *targets)

            request = replace(
                request,
                tool_name=tool_name,
                tool_use_id=call.tool_use_id,
                targets=targets,
            )

            context.permission_context.authorize(request)

            result = tool.run(prepared_input, context)
            return replace(
                result,
                name=tool_name,
                tool_use_id=call.tool_use_id,
            )

        except (KeyboardInterrupt, EOFError):
            raise
        except ToolPermissionError as e:
            return error_result(f"Tool {tool_name} permission denied: {e}")
        except ToolInputError as e:
            return error_result(f"Tool {tool_name} invalid input: {e}")
        except Exception as e:
            return error_result(f"Tool {tool_name} failed: {e}")
        finally:
              context.active_tool_use_id = previous_tool_use_id
