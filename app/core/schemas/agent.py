from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type AgentDecision = Literal["respond", "clarify", "tool", "prepare", "error"]


class ResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["respond", "clarify"]
    text: str = Field(min_length=1)


class AgentResult(BaseModel):
    text: str
    decision: AgentDecision = "respond"
    confirmation_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    reason: str = "completed"
    truncated: bool = False
    planned: bool = False

    @model_validator(mode="after")
    def validate_confirmation(self) -> "AgentResult":
        if self.decision == "prepare" and not self.planned and not self.confirmation_id:
            raise ValueError("prepared action requires confirmation_id")
        if self.confirmation_id and (self.decision != "prepare" or self.planned):
            raise ValueError("confirmation_id is only valid for a prepared action")
        return self


class EventContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: int
    event_id: str
    request_id: str


@dataclass(frozen=True)
class Acquired:
    calls_used: int = 0


@dataclass(frozen=True)
class Replay:
    result: AgentResult


@dataclass(frozen=True)
class InProgress:
    pass


type TurnClaim = Acquired | Replay | InProgress
type DialogResult = AgentResult | InProgress


@dataclass(frozen=True)
class Final:
    result: AgentResult

    def __post_init__(self) -> None:
        if self.result.decision == "prepare" and not self.result.planned:
            raise ValueError("saved confirmation requires AwaitConfirmation")


@dataclass(frozen=True)
class AwaitConfirmation:
    result: AgentResult

    def __post_init__(self) -> None:
        if self.result.decision != "prepare" or not self.result.confirmation_id:
            raise ValueError("AwaitConfirmation requires a saved confirmation")


@dataclass(frozen=True)
class Continue:
    pass


type StepOutcome = Continue | Final | AwaitConfirmation
