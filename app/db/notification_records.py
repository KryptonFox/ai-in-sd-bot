from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class NotificationKind(StrEnum):
    REMINDER = "reminder"
    DAILY_BRIEFING = "daily_briefing"


class NotificationState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    SENT = "sent"
    FAILED = "failed"
    MISSED = "missed"
    SUPPRESSED = "suppressed"


class CitySelectionState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class CityRecord:
    name: str
    country: str
    region: str | None
    latitude: float
    longitude: float


@dataclass(frozen=True)
class NotificationSettingsRecord:
    user_id: int
    destination_chat_id: int | None
    city: CityRecord | None
    city_confirmed_at: datetime | None
    briefing_enabled: bool
    briefing_consent_at: datetime | None
    timezone: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class CitySelectionRecord:
    id: UUID
    user_id: int
    candidates: tuple[CityRecord, ...]
    state: CitySelectionState
    created_at: datetime
    expires_at: datetime
    selected_index: int | None


@dataclass(frozen=True)
class OutgoingNotificationRecord:
    id: UUID
    event_key: str
    user_id: int
    kind: NotificationKind
    destination_chat_id: int | None
    payload: dict[str, object]
    state: NotificationState
    attempt_count: int
    scheduled_for: datetime
    next_attempt_at: datetime
    lease_id: UUID
    trace_id: UUID
    lease_until: datetime | None = None
