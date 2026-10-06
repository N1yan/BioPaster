import json
import re
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4
from pydantic import BaseModel


def _serialize(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, BaseModel):
        return dict(value)

    raise TypeError(
        f"Cannot serialize {type(value).__name__}"
    )
    
    
class SessionLog:
    def __init__(
        self,
        root: Path,
        *,
        session_id: str | None = None,
    ):
        resuming = session_id is not None

        if resuming:
            if (
                not isinstance(session_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", session_id) is None
            ):
                raise ValueError("Invalid session ID.")

        self.session_id = session_id if resuming else uuid4().hex
        self.run_id: str | None = None

        self.directory = Path(root) / self.session_id

        if resuming:
            if not self.directory.is_dir():
                raise FileNotFoundError(
                    f"Session directory does not exist: {self.directory}"
                )
        else:
            self.directory.mkdir(
                mode=0o700,
                parents=True,
                exist_ok=False,
            )

        self.path = self.directory / "events.jsonl"
        self._lock = Lock()

        if resuming:
            log_missing = not self.path.exists()
            self.path.touch(mode=0o600, exist_ok=True)

            if log_missing:
                self.record(
                    "session_log_created",
                    {"reason": "Log file was missing when opening a saved session."},
                )
        else:
            self.path.touch(mode=0o600, exist_ok=False)
            self.record("session_start", {})

    def record(self, event_type: str, data):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "type": event_type,
            "data": data,
        }

        line = json.dumps(
            event,
            ensure_ascii=False,
            default=_serialize,
        )

        with self._lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")

    def record_exception(self, event_type: str, exc: BaseException):
        self.record(
            event_type,
            {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(
                    traceback.format_exception(
                        type(exc), exc, exc.__traceback__
                    )
                ),
            },
        )
    
    @classmethod
    def open_existing(cls, root: Path, session_id: str) -> "SessionLog":
        """Open a saved session's log for subsequent appends."""
        return cls(root, session_id=session_id)

    def start_run(self, user_input: str):
        self.run_id = uuid4().hex
        self.record("run_start", {"user_input": user_input})

    def end_run(self, status: str):
        self.record("run_end", {"status": status})
        self.run_id = None