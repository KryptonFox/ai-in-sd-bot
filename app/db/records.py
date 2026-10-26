from datetime import datetime
from typing import Literal, NotRequired, TypedDict

type ConfirmationState = Literal[
    "pending",
    "confirmed",
    "executing",
    "succeeded",
    "cancelled",
    "expired",
    "failed",
]


class ConfirmationRecord(TypedDict):
    id: str
    user_id: int
    event_id: str
    position: int
    tool_name: str
    arguments: dict[str, str]
    timezone: str
    idempotency_key: str
    state: ConfirmationState
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    execution_started_at: datetime | None
    updated_at: datetime
    calls_remaining: int
    request_id: str
    result: dict[str, object] | None
    error_code: str | None
    claimed: NotRequired[bool]


class EventClaim(TypedDict):
    claimed: bool
    calls_used: NotRequired[int]
    result: NotRequired[dict[str, object]]


class DecisionAudit(TypedDict):
    id: int
    user_id: int
    event_id: str
    request_id: str
    decision: str
    tool_name: str | None
    arguments: dict[str, str]
    validation: str
    execution: str
    duration_ms: float
    reason: str
    created_at: datetime
