import json
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
    def __init__(self, root: Path):
        self.session_id = uuid4().hex
        self.run_id: str | None = None

        self.directory = root / self.session_id
        self.directory.mkdir(parents=True, exist_ok=False)

        self.path = self.directory / "events.jsonl"
        self.path.touch(mode=0o600, exist_ok=False)

        self._lock = Lock()
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

    def start_run(self, user_input: str):
        self.run_id = uuid4().hex
        self.record("run_start", {"user_input": user_input})

    def end_run(self, status: str):
        self.record("run_end", {"status": status})
        self.run_id = None