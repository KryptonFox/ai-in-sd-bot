from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RouteTarget(StrEnum):
    SCHEDULE = "schedule"
    KNOWLEDGE = "knowledge"
    LIFESTYLE = "lifestyle"
    MATH = "math"
    CLARIFY = "clarify"
    LEGACY = "legacy"


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    target: RouteTarget
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
