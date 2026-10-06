from dataclasses import dataclass
from pathlib import Path
import re
import json
import os
import tempfile
from datetime import datetime
from .conversation import Conversation




@dataclass
class SessionSnapshot:
    session_id: str
    created_at: str
    updated_at: str

    provider: str
    model: str

    workspace_root: Path
    cwd: Path
    notebook_path: Path | None

    task_list_id: str
    conversation: Conversation

    interrupted: bool = False
    version: int = 1
    title: str | None = None
    title_attempted: bool = False

    def to_dict(self) -> dict:
        """Convert the snapshot into JSON-compatible data."""
        return {
            "version": self.version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "workspace_root": str(self.workspace_root),
            "cwd": str(self.cwd),
            "notebook_path": (
                str(self.notebook_path)
                if self.notebook_path is not None
                else None
            ),
            "task_list_id": self.task_list_id,
            "conversation": self.conversation.to_dict(),
            "interrupted": self.interrupted,
            "title": self.title,
            "title_attempted": self.title_attempted,
        }
        
    @classmethod
    def from_dict(cls, data: dict) -> "SessionSnapshot":
        """Validate and restore a session snapshot."""
        if not isinstance(data, dict):
            raise ValueError("Session snapshot must be an object.")

        version = data.get("version")
        if type(version) is not int or version != 1:
            raise ValueError("Unsupported session snapshot version.")

        for key in ("session_id", "task_list_id"):
            value = data.get(key)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{32}", value) is None
            ):
                raise ValueError(
                    f"{key} must be a 32-character lowercase hexadecimal ID."
                )

        if data["task_list_id"] != data["session_id"]:
            raise ValueError(
                "task_list_id must match session_id in this version."
            )

        for key in ("created_at", "updated_at"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be an ISO timestamp.")

            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"{key} must be an ISO timestamp."
                ) from exc

        for key in ("provider", "model"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string.")

        paths = {}

        for key in ("workspace_root", "cwd", "notebook_path"):
            if key not in data:
                raise ValueError(f"Missing snapshot field: {key}")

            value = data[key]

            if key == "notebook_path" and value is None:
                paths[key] = None
                continue

            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
            ):
                raise ValueError(f"{key} must be an absolute path.")

            path = Path(value)
            if not path.is_absolute():
                raise ValueError(f"{key} must be an absolute path.")

            paths[key] = path

        interrupted = data.get("interrupted", False)
        if not isinstance(interrupted, bool):
            raise ValueError("interrupted must be a boolean.")

        conversation = Conversation.from_dict(data.get("conversation"))
        title = data.get("title")
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError("title must be non-empty text or null.")
        title_attempted = data.get("title_attempted", bool(conversation.messages))
        if not isinstance(title_attempted, bool):
            raise ValueError("title_attempted must be a boolean.")

        return cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            provider=data["provider"],
            model=data["model"],
            workspace_root=paths["workspace_root"],
            cwd=paths["cwd"],
            notebook_path=paths["notebook_path"],
            task_list_id=data["task_list_id"],
            conversation=conversation,
            interrupted=interrupted,
            version=version,
            title=title,
            title_attempted=title_attempted,
        )
        
        

def _session_path(session_id: str) -> Path:
    """Build the snapshot path from a validated session ID."""
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", session_id) is None
    ):
        raise ValueError(
            "session_id must be a 32-character lowercase hexadecimal ID."
        )

    return (
        Path.home()
        / ".biopaster"
        / "sessions"
        / session_id
        / "session.json"
    )


def save_session(snapshot: SessionSnapshot) -> None:
    """Validate and atomically save a session snapshot."""
    data = snapshot.to_dict()

    # Validate before touching the destination file.
    SessionSnapshot.from_dict(data)
    content = json.dumps(data, ensure_ascii=False, indent=2)

    path = _session_path(snapshot.session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".session-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)

        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_session(session_id: str) -> SessionSnapshot:
    """Read and validate a saved session without changing runtime state."""
    path = _session_path(session_id)

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    snapshot = SessionSnapshot.from_dict(data)

    if snapshot.session_id != session_id:
        raise ValueError(
            "Snapshot session_id does not match its directory."
        )

    return snapshot


def list_sessions() -> tuple[list[dict], list[str]]:
    """List recoverable snapshots and report invalid entries."""
    root = Path.home() / ".biopaster" / "sessions"
    sessions = []
    diagnostics = []

    if not root.exists():
        return sessions, diagnostics

    for directory in root.iterdir():
        if not directory.is_dir():
            continue

        # Only inspect directories using the current session-ID format.
        if re.fullmatch(r"[0-9a-f]{32}", directory.name) is None:
            continue

        path = directory / "session.json"

        if not path.exists():
            continue

        try:
            snapshot = load_session(directory.name)
            if not snapshot.conversation.messages:
                continue
            preview = ""
            for message in snapshot.conversation.messages:
                if message.role == "user" and not message._is_internal and isinstance(message.content, str):
                    preview = " ".join(message.content.split())[:100]
                    preview = "".join(char for char in preview if char.isprintable())
                    break

            sessions.append({
                "session_id": snapshot.session_id,
                "updated_at": snapshot.updated_at,
                "workspace_root": str(snapshot.workspace_root),
                "model": snapshot.model,
                "message_count": len(snapshot.conversation.messages),
                "interrupted": snapshot.interrupted,
                "preview": preview,
                "title": snapshot.title,
            })
        except (OSError, ValueError) as exc:
            diagnostics.append(
                f"{directory.name}: {type(exc).__name__}: {exc}"
            )

    sessions.sort(key=lambda item: item["session_id"])
    return sessions, diagnostics
