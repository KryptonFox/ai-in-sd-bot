from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["respond", "clarify"]
    text: str = Field(min_length=1, max_length=20000)


class AgentResult(BaseModel):
    text: str
    decision: Literal["respond", "clarify", "tool", "prepare", "error"] = "respond"
    confirmation_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class EventContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: int
    event_id: str
    request_id: str
