from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.schemas.agent import AgentDecision, AgentResult
from app.core.schemas.knowledge import MaterialSnippet, WebSearchItem


class SpecialistIntent(StrEnum):
    USER_QUERY = "user_query"
    DAILY_BRIEFING = "daily_briefing"


class SpecialistSource(StrEnum):
    TELEGRAM = "telegram"
    BACKGROUND = "background"


class SpecialistRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    actor_id: int = Field(gt=0)
    request_id: str = Field(min_length=1)
    trace_id: UUID
    started_at: datetime
    timezone: ZoneInfo
    text: str
    intent: SpecialistIntent = SpecialistIntent.USER_QUERY
    source: SpecialistSource = SpecialistSource.TELEGRAM

    @field_validator("actor_id", mode="before")
    @classmethod
    def reject_boolean_actor_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("actor_id must be an integer")
        return value

    @field_validator("started_at")
    @classmethod
    def require_aware_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        return value


class KnowledgeFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["knowledge"] = "knowledge"
    source: Annotated[MaterialSnippet | WebSearchItem, Field(discriminator="source_type")]


class ScheduleFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["schedule"] = "schedule"
    source: Literal["get_schedule", "get_schedule_week", "schedule://current-week"]
    data: dict[str, object]


class WeatherFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["weather"] = "weather"
    data: dict[str, object]


class MathFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["math"] = "math"
    operation: Literal["math_calculate", "math_solve", "math_plot"]
    data: dict[str, object]


type SpecialistFact = Annotated[
    KnowledgeFact | ScheduleFact | WeatherFact | MathFact,
    Field(discriminator="kind"),
]


class SpecialistResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AgentDecision
    public_text: str
    facts: tuple[SpecialistFact, ...]
    used_tools: tuple[str, ...] = ()
    error_code: str | None = None
    agent_result: AgentResult = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_status_contract(self) -> "SpecialistResult":
        if self.status is not self.agent_result.decision:
            raise ValueError("status must match agent_result decision")
        if self.status is AgentDecision.ERROR and not self.error_code:
            raise ValueError("error status requires error_code")
        if self.status is not AgentDecision.ERROR and self.error_code is not None:
            raise ValueError("error_code is only valid for error status")
        if self.status is AgentDecision.PREPARE and self.agent_result.confirmation_id is None:
            raise ValueError("prepare status requires confirmation_id")
        if (
            self.agent_result.confirmation_id is not None
            and self.status is not AgentDecision.PREPARE
        ):
            raise ValueError("confirmation_id requires prepare status")
        return self

    def to_agent_result(self) -> AgentResult:
        return self.agent_result.model_copy(
            update={"text": self.public_text, "decision": self.status}
        )
