from dataclasses import dataclass, field
from pathlib import Path
from .errors import ToolPermissionError
from typing import Callable


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

@dataclass
class ToolPermissionContext:
    workspace_root: Path | None = None
    additional_working_directories: tuple[Path, ...] = ()
    permission_handler: Callable[[str], bool] | None = field(
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
    
    def execute_permission_check(self, command: str):
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
            
        # ask user for confirmation if the command contains potentially dangerous keywords
        POTENTIALLY_DANGEROUS_PATTERNS = [
            re.compile(r"\brm\b", re.IGNORECASE),
            re.compile(r"\bchmod\b", re.IGNORECASE),
            re.compile(r"\binstall\b", re.IGNORECASE),
            re.compile(r"\buninstall\b", re.IGNORECASE),
            re.compile(r"\bwget\b", re.IGNORECASE),
            re.compile(r"\bcurl\b", re.IGNORECASE),
            re.compile(r"\bkill\b", re.IGNORECASE),
            re.compile(r"\bpkill\b", re.IGNORECASE),
        ]
        
        # Ask for confirmation if any potentially dangerous command is detected
        LANGUAGE_INJECTION_PATTERNS = [
            # interpreters
            re.compile(r"\bpython\b", re.IGNORECASE),
            re.compile(r"\bpython2\b", re.IGNORECASE),
            re.compile(r"\bpython3\b", re.IGNORECASE),
            re.compile(r"\bperl\b", re.IGNORECASE),
            re.compile(r"\bruby\b", re.IGNORECASE),
            re.compile(r"\bnode\b", re.IGNORECASE),
            re.compile(r"\bdeno\b", re.IGNORECASE),
            re.compile(r"\btsx\b", re.IGNORECASE),
            re.compile(r"\bphp\b", re.IGNORECASE),
            re.compile(r"\blua\b", re.IGNORECASE),
            # package runners
            re.compile(r"\bnpx\b", re.IGNORECASE),
            re.compile(r"\bbunx\b", re.IGNORECASE),
            re.compile(r"\bnpm run\b", re.IGNORECASE),
            re.compile(r"\byarn run\b", re.IGNORECASE),
            re.compile(r"\bpnpm run\b", re.IGNORECASE),
            re.compile(r"\bbun run\b", re.IGNORECASE),
            # shells reachable from both (Git Bash / WSL on Windows, native on Unix)
            re.compile(r"\bbash\b", re.IGNORECASE),
            re.compile(r"\bsh\b", re.IGNORECASE),
            # remote arbitrary-command wrapper (native OpenSSH on Win10+)
            re.compile(r"\bssh\b", re.IGNORECASE),   
        ]

        for pattern in POTENTIALLY_DANGEROUS_PATTERNS + LANGUAGE_INJECTION_PATTERNS:
            if pattern.search(command):
                message = (
                    f"{command}\nwill be executed. Are you sure you want to proceed ?"
                )
                # response = _ask_permission(message)
                response = self.permission_handler(message)

                if not response:
                    raise ToolPermissionError(f"Command execution aborted by user: {command}")