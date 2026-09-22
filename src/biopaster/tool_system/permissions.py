from dataclasses import dataclass, field
from pathlib import Path
from .errors import ToolPermissionError
from typing import Callable, Literal, TYPE_CHECKING
if TYPE_CHECKING:
    from ..agent.session_log import SessionLog

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

def _resolve_path(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()

# def _ask_permission(message: str) -> bool:
#     msg_highlighted = f"\033[33m{message}\033[0m"
#     prompt_highlighted = f"\033[96mAllow: (y/n): \033[0m"
#     response = input(f"{msg_highlighted}\n{prompt_highlighted}").strip().lower()
#     return response in {"y", "yes"}

@dataclass(frozen=True)
class PermissionRule:
    tool_name: str
    rule_content: str | None = None
    behavior: Literal["allow", "ask", "deny"] = "allow"
    source: Literal["session"] = "session"

@dataclass(frozen=True)
class PermissionDecision:
    behavior: Literal["allow", "ask", "deny"]
    reason: str
    matched_rule: PermissionRule | None = None
    
@dataclass(frozen=True)
class PermissionTarget:
    tool_name: str
    rule_content: str | None
    
@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    tool_use_id: str | None
    description: str
    targets: tuple[PermissionTarget, ...]
    suggestions: tuple[PermissionRule, ...]

@dataclass(frozen=True)
class PermissionAnswer:
    allowed: bool
    rules: tuple[PermissionRule, ...] = ()

@dataclass
class ToolPermissionContext:
    workspace_root: Path | None = None
    additional_working_directories: tuple[Path, ...] = ()
    permission_handler: Callable[
        [PermissionRequest], PermissionAnswer
      ] | None = field(
          default=None,
          repr=False,
          compare=False,
      )
    session_rules: list[PermissionRule] = field(
        default_factory=list,
        repr=False,
    )
    session_log: "SessionLog | None" = field(
        default=None,
        repr=False,
        compare=False,
    )
    
    def __post_init__(self) -> None:
        if self.workspace_root is not None:
            self.workspace_root = _resolve_path(self.workspace_root)

        self.additional_working_directories = tuple(
            _resolve_path(path)
            for path in self.additional_working_directories
        )

    def allowed_path(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        if self.workspace_root:
            roots.append(self.workspace_root)
        roots.extend(self.additional_working_directories)
        return tuple(roots)
    
    def ensure_path_allowed(self, path: str | Path) -> Path:
        resolved = _resolve_path(path)
        allowed = self.allowed_path()
        if any(_is_within(resolved, p) for p in allowed):
            return resolved
        allowed_str = ", ".join(str(p) for p in allowed)
        raise ToolPermissionError(f"path is outside allowed working directories: {resolved} (allowed: {allowed_str})")
    
    def check_command_restrictions(self, command: str) -> None:
        import re
        
        # dangerous commands that should not be allowed
        DANGEROUS_PATTERNS = [
            re.compile(r"\bsudo\b", re.IGNORECASE),
            re.compile(r"\bshutdown\b", re.IGNORECASE),
            re.compile(r"\breboot\b", re.IGNORECASE),
            re.compile(r"\bmkfs\b", re.IGNORECASE),
            re.compile(r"\bdd\b\s+if=", re.IGNORECASE),
            re.compile(r"\brm\b.*\s+-rf\s+/\s*$", re.IGNORECASE),
            re.compile(r"\brm\b.*\s+-rf\s+/\s+"),
            re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
        ]
        
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(command):
                raise ToolPermissionError(f"Command is not allowed due to security restrictions: {command}")
    
    def add_rule(self, rule: PermissionRule) -> None:
        if rule.behavior not in {"allow", "ask", "deny"}:
            raise ValueError(
                f"Invalid permission behavior: {rule.behavior!r}"
            )

        if rule.source != "session":
            raise ValueError(
                f"Unsupported permission source: {rule.source!r}"
            )

        tool_name = rule.tool_name.strip()
        if not tool_name:
            raise ValueError("Tool name cannot be empty.")

        normalized_rule = PermissionRule(
            tool_name=tool_name.lower(),
            rule_content=rule.rule_content,
            behavior=rule.behavior,
            source=rule.source,
        )

        if normalized_rule not in self.session_rules:
            self.session_rules.append(normalized_rule)

    def remove_rule(self, rule: PermissionRule) -> None:
        if rule in self.session_rules:
            self.session_rules.remove(rule)
    def clear_session_rules(self) -> None:
        self.session_rules.clear()

    def _rule_matches(
        self,
        rule: PermissionRule,
        target: PermissionTarget,
    ) -> bool:
        if rule.tool_name.lower() != target.tool_name.lower():
            return False

        # A rule without content applies to the entire tool.
        if rule.rule_content is None:
            return True

        if target.rule_content is None:
            return False

        tool_name = target.tool_name.lower()

        # Non-file targets use exact content matching.
        if tool_name not in {"read", "edit"}:
            return rule.rule_content == target.rule_content

        # File targets must already be absolute and normalized.
        target_path = Path(target.rule_content)
        directory_rule = rule.rule_content.endswith("/**")
        rule_path = Path(
            rule.rule_content[:-2]
            if directory_rule
            else rule.rule_content
        )

        if not target_path.is_absolute() or not rule_path.is_absolute():
            raise ValueError(
                "File permission rules and targets must use absolute paths."
            )

        if directory_rule:
            return _is_within(target_path, rule_path)

        return target_path == rule_path
    
    def evaluate(
        self,
        target: PermissionTarget,
    ) -> PermissionDecision:
        matching_rules = [
            rule
            for rule in self.session_rules
            if self._rule_matches(rule, target)
        ]

        for behavior in ("deny", "ask", "allow"):
            for rule in matching_rules:
                if rule.behavior == behavior:
                    return PermissionDecision(
                        behavior=rule.behavior,
                        reason=(
                            f"Matched a {rule.source} "
                            f"{rule.behavior} rule."
                        ),
                        matched_rule=rule,
                    )
                    
        if target.tool_name.lower() in {
              "websearch",
              "webfetch",
              "papersearch",
              "paperfetch",
        }:
            return PermissionDecision(
                behavior="allow",
                reason="Network read tools are allowed by default.",
            )
    
        if (
              target.tool_name.lower() in {"read", "edit"}
              and target.rule_content is not None
        ):
            path = Path(target.rule_content)

            if not path.is_absolute():
                raise ValueError("File permission targets must use absolute paths.")

            if any(
                _is_within(path, root)
                for root in self.allowed_path()
            ):
                return PermissionDecision(
                    behavior="allow",
                    reason="Path is within an allowed working directory.",
                )

        return PermissionDecision(
            behavior="ask",
            reason="No matching permission rule.",
        )
        
        
    def authorize(self, request: PermissionRequest) -> None:
        if self.session_log is not None:
            self.session_log.record(
                "permission_request",
                {
                    "tool_name": request.tool_name,
                    "tool_use_id": request.tool_use_id,
                    "request": request,
                },
            )
        if not request.targets:
            raise ToolPermissionError(
                "Permission request must contain at least one target."
            )

        decisions = [
            self.evaluate(target)
            for target in request.targets
        ]
        if self.session_log is not None:
            self.session_log.record(
                "permission_evaluation",
                {
                    "tool_name": request.tool_name,
                    "tool_use_id": request.tool_use_id,
                    "targets": request.targets,
                    "decisions": decisions,
                },
            )

        # A denial takes precedence over approvals and confirmation.
        for decision in decisions:
            if decision.behavior == "deny":
                self.record_permission_decision(
                    tool_name=request.tool_name,
                    tool_use_id=request.tool_use_id,
                    outcome="denied",
                    reason=decision.reason,
                )
                raise ToolPermissionError(decision.reason)

        if all(
            decision.behavior == "allow"
            for decision in decisions
        ):
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="allowed",
                reason="All permission targets were automatically allowed.",
            )
            return

        if self.permission_handler is None:
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="blocked",
                reason="Permission confirmation is required but no handler is available.",
            )
            raise ToolPermissionError(
                f"Permission confirmation is unavailable for "
                f"{request.tool_name}."
            )

        try:
            answer = self.permission_handler(request)
        except (KeyboardInterrupt, EOFError):
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="interrupted",
                reason="Permission confirmation was interrupted.",
            )
            raise

        if not isinstance(answer, PermissionAnswer):
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="error",
                reason="Permission handler returned an invalid answer type.",
            )
            raise ToolPermissionError(
                "Permission handler must return PermissionAnswer."
            )

        if not answer.allowed:
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="denied",
                reason="User denied permission.",
            )
            raise ToolPermissionError(
                f"User denied permission for {request.tool_name}."
            )

        # Validate every rule before storing any of them.
        for rule in answer.rules:
            if (
                rule not in request.suggestions
                or rule.behavior != "allow"
                or rule.source != "session"
                or not rule.tool_name.strip()
            ):
                self.record_permission_decision(
                    tool_name=request.tool_name,
                    tool_use_id=request.tool_use_id,
                    outcome="error",
                    reason="Permission answer contained an invalid approval rule.",
                )
                raise ToolPermissionError(
                    "Invalid permission approval rule."
                )

        previous_rules = self.session_rules.copy()
        try:
            for rule in answer.rules:
                self.add_rule(rule)
        except Exception as exc:
            self.session_rules[:] = previous_rules
            self.record_permission_decision(
                tool_name=request.tool_name,
                tool_use_id=request.tool_use_id,
                outcome="error",
                reason=f"Failed to save session rules: {exc}",
            )
            raise

        self.record_permission_decision(
            tool_name=request.tool_name,
            tool_use_id=request.tool_use_id,
            outcome="allowed",
            reason=(
                "User approved permission and selected session rules."
                if answer.rules
                else "User approved permission for this call only."
            ),
        )
            
    def record_permission_decision(
        self,
        *,
        tool_name: str,
        tool_use_id: str | None,
        outcome: str,
        reason: str,
    ) -> None:
        if self.session_log is not None:
            self.session_log.record(
                "permission_decision",
                {
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "outcome": outcome,
                    "reason": reason,
                },
            )