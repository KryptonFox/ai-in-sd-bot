from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentDecision(StrEnum):
    RESPOND = "respond"
    CLARIFY = "clarify"
    TOOL = "tool"
    PREPARE = "prepare"
    ERROR = "error"


class TurnState(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ClarificationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)


class AgentResult(BaseModel):
    text: str
    decision: AgentDecision = AgentDecision.RESPOND
    confirmation_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    reason: str = "completed"
    truncated: bool = False
    resource_uris: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_confirmation(self) -> "AgentResult":
        if self.decision == AgentDecision.PREPARE and not self.confirmation_id:
            raise ValueError("prepared action requires confirmation_id")
        if self.confirmation_id and self.decision != AgentDecision.PREPARE:
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
