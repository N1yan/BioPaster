"""User-visible turns, independent of the messages sent to the model."""

from dataclasses import asdict, dataclass, field


@dataclass
class ReplayTurn:
    user: str
    answer: str = ""
    status: str = "pending"
    errors: list[str] = field(default_factory=list)


@dataclass
class ReplayHistory:
    turns: list[ReplayTurn] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "ReplayHistory":
        if not isinstance(data, dict):
            raise ValueError("Session requires replay history.")
        if not isinstance(data.get("turns"), list):
            raise ValueError("Replay turns must be a list.")
        turns = []
        for item in data["turns"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid replay turn.")
            if not isinstance(item.get("user"), str) or not isinstance(item.get("answer"), str):
                raise ValueError("Replay messages must be text.")
            if item.get("status") not in ("pending", "success", "error", "interrupted"):
                raise ValueError("Invalid replay status.")
            errors = item.get("errors")
            if not isinstance(errors, list) or any(not isinstance(e, str) for e in errors):
                raise ValueError("Replay errors must be a list of text.")
            turns.append(ReplayTurn(item["user"], item["answer"], item["status"], list(errors)))
        return cls(turns=turns)
