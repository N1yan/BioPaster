from dataclasses import dataclass,field
from pathlib import Path
from typing import Any
from .permissions import ToolPermissionContext
from ..agent.session_log import SessionLog


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
   kernels: dict[str, Any] = field(default_factory=dict)
   session_log: SessionLog | None = None

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
   
   def execute_permission_check(self, command: str):
      self.permission_context.execute_permission_check(command)
         
      
