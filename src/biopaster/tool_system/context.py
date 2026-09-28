from dataclasses import dataclass,field
from pathlib import Path
from typing import Any, Literal
from .permissions import (
   ToolPermissionContext,
   PermissionTarget,
   PermissionDecision,
)
from ..agent.session_log import SessionLog
import os


@dataclass
class ToolContext:
   workspace_root: Path
   permission_context: ToolPermissionContext = field(default_factory=ToolPermissionContext)
   cwd: Path | None = None
   read_file_fingerprints: dict[Path, tuple[int, int]] = field(default_factory=dict)
   mcp_clients:dict[str, Any] = field(default_factory=dict)
   todos: list[dict[str, Any]] = field(default_factory=list)
   tools: list[str] = field(default_factory=list)
   notebook_path: Path | None = None
   session_log: SessionLog | None = None
   active_tool_use_id: str | None = None

   def __post_init__(self):
      self.workspace_root = Path(self.workspace_root).resolve()
      if self.cwd is None:
            self.cwd = self.workspace_root
      else:
            self.cwd = Path(self.cwd).resolve()
      if self.permission_context.workspace_root is None:
         self.permission_context.workspace_root = self.workspace_root
         
   def mark_file_read(self, path: Path):
      stat = path.stat()
      self.read_file_fingerprints[path.resolve()] = (int(stat.st_mtime_ns), int(stat.st_size))
      
   def was_file_read_and_unchanged(self, path: Path) -> bool:
      resolved = path.resolve()
      fingerprint = self.read_file_fingerprints.get(resolved)
      if not fingerprint:
         return False
      stat = resolved.stat()
      return fingerprint == (int(stat.st_mtime_ns), int(stat.st_size))
      
   def ensure_allowed_path(self, path: str | Path) -> Path:
      p = Path(path).expanduser()
      if not p.is_absolute():
         base = self.cwd
         p = (base / p).resolve()
      return self.permission_context.ensure_path_allowed(p)
   
   def resolve_permission_paths(
         self,
         path: str | Path,
   ) -> tuple[Path, ...]:
      requested = Path(path).expanduser()

      if not requested.is_absolute():
            requested = self.cwd / requested

      # Resolve before normalizing away "..", because a preceding
      # directory may be a symbolic link.
      resolved = requested.resolve()
      absolute = Path(os.path.abspath(requested))

      if absolute == resolved:
            return (resolved,)

      return (absolute, resolved)
   
   def check_file_permission(
      self,
      path: str | Path,
      operation: Literal["read", "edit"],
   ) -> PermissionDecision:
      if operation not in {"read", "edit"}:
            raise ValueError(
               f"Unsupported file operation: {operation!r}"
            )

      tool_name = "Read" if operation == "read" else "Edit"
      paths = self.resolve_permission_paths(path)

      decisions = [
            self.permission_context.evaluate(
               PermissionTarget(
                  tool_name=tool_name,
                  rule_content=str(candidate),
               )
            )
            for candidate in paths
      ]

      for behavior in ("deny", "ask"):
            for candidate, decision in zip(paths, decisions):
               if decision.behavior == behavior:
                  return PermissionDecision(
                        behavior=decision.behavior,
                        reason=f"{candidate}: {decision.reason}",
                        matched_rule=decision.matched_rule,
                  )

      return PermissionDecision(
            behavior="allow",
            reason="All file paths passed permission checks.",
      )
   
