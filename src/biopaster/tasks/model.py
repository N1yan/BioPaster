from dataclasses import dataclass, field
from typing import Literal


TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class Task:
    id: str
    subject: str
    description: str
    activeForm: str = ""
    status: TaskStatus = "pending"
    blockedBy: list[str] = field(default_factory=list)