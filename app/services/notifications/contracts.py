from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID

from app.core.trace import OperationTrace
from app.db.notification_records import (
    CityRecord,
    CitySelectionRecord,
    NotificationSettingsRecord,
    NotificationState,
    OutgoingNotificationRecord,
)


class NotificationFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotificationDeliveryError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


type BotDelivery = Callable[[int, str], Awaitable[None]]
type TraceSink = Callable[[OperationTrace], Awaitable[None]]


@dataclass(frozen=True)
class BriefingSection:
    title: str
    text: str | None = None
    problem: str | None = None


@dataclass(frozen=True)
class BriefingContent:
    sections: tuple[BriefingSection, ...] = ()


@dataclass(frozen=True)
class BriefingContext:
    user_id: int
    trace_id: UUID
    local_date: date
    timezone: str
    city: CityRecord
    scheduled_for: datetime
    prepared_at: datetime


type BriefingProvider = Callable[[BriefingContext], Awaitable[BriefingContent]]


class CityResolver(Protocol):
    async def resolve(self, query: str) -> Sequence[CityRecord]: ...


@dataclass(frozen=True)
class NotificationPolicies:
    tick_interval_seconds: float = 30.0
    reminder_grace: timedelta = timedelta(minutes=15)
    briefing_start: time = time(8, 0)
    briefing_end: time = time(9, 0)
    max_attempts: int = 3
    retry_delays: tuple[timedelta, ...] = (
        timedelta(seconds=30),
        timedelta(minutes=2),
    )
    lease_duration: timedelta = timedelta(seconds=60)
    delivery_timeout: timedelta = timedelta(seconds=30)
    briefing_deadline: timedelta = timedelta(seconds=25)
    batch_size: int = 50
    city_selection_ttl: timedelta = timedelta(minutes=5)
    city_candidate_limit: int = 5

    def __post_init__(self) -> None:
        if self.tick_interval_seconds <= 0:
            raise ValueError("tick interval must be positive")
        if self.reminder_grace <= timedelta(0):
            raise ValueError("reminder grace must be positive")
        if self.max_attempts < 1:
            raise ValueError("max attempts must be positive")
        if not self.retry_delays:
            raise ValueError("retry delays are required")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        if self.delivery_timeout <= timedelta(0):
            raise ValueError("delivery timeout must be positive")
        if self.briefing_deadline <= timedelta(0):
            raise ValueError("briefing deadline must be positive")
        if self.batch_size < 1:
            raise ValueError("batch size must be positive")
        if self.city_selection_ttl <= timedelta(0):
            raise ValueError("city selection ttl must be positive")
        if self.city_candidate_limit < 1:
            raise ValueError("city candidate limit must be positive")


@dataclass(frozen=True)
class NotificationSettingsView:
    destination_saved: bool
    timezone: str | None
    city: CityRecord | None
    briefing_enabled: bool


@dataclass(frozen=True)
class CitySelection:
    id: UUID
    candidates: tuple[CityRecord, ...]
    expires_at: datetime


@dataclass(frozen=True)
class TickReport:
    prepared_reminders: int = 0
    prepared_briefings: int = 0
    recovered_leases: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    missed: int = 0
    suppressed: int = 0


@dataclass(frozen=True)
class BlockedDelivery:
    state: NotificationState
    code: str


class NotificationSettingsReader(Protocol):
    async def settings(self, user_id: int) -> NotificationSettingsRecord | None: ...


class NotificationPreferencesRepository(NotificationSettingsReader, Protocol):
    async def remember_private_chat(
        self,
        user_id: int,
        chat_id: int,
        now: datetime,
    ) -> None: ...

    async def create_city_selection(self, selection: CitySelectionRecord) -> None: ...

    async def city_selection(self, selection_id: UUID) -> CitySelectionRecord | None: ...

    async def confirm_city_selection(
        self,
        selection_id: UUID,
        user_id: int,
        index: int,
        now: datetime,
    ) -> NotificationSettingsRecord | None: ...

    async def cancel_city_selection(
        self,
        selection_id: UUID,
        user_id: int,
        now: datetime,
    ) -> bool: ...

    async def set_briefing_enabled(
        self,
        user_id: int,
        enabled: bool,
        now: datetime,
    ) -> NotificationSettingsRecord | None: ...


class NotificationDeliveryRepository(NotificationSettingsReader, Protocol):
    async def prepare_reminder_events(self, now: datetime, grace: timedelta) -> int: ...

    async def briefing_candidates(self) -> list[NotificationSettingsRecord]: ...

    async def create_briefing_event(
        self,
        user_id: int,
        local_date: date,
        timezone: str,
        scheduled_for: datetime,
        trace_id: UUID,
        now: datetime,
    ) -> bool: ...

    async def recover_expired_leases(self, now: datetime, max_attempts: int) -> int: ...

    async def claim_due_event(
        self,
        now: datetime,
        lease_until: datetime,
        max_attempts: int,
    ) -> OutgoingNotificationRecord | None: ...

    async def save_payload(
        self,
        event_id: UUID,
        lease_id: UUID,
        payload: dict[str, object],
        now: datetime,
    ) -> bool: ...

    async def mark_sent(self, event_id: UUID, lease_id: UUID, now: datetime) -> bool: ...

    async def mark_retry(
        self,
        event_id: UUID,
        lease_id: UUID,
        next_attempt_at: datetime,
        code: str,
        now: datetime,
    ) -> bool: ...

    async def mark_terminal(
        self,
        event_id: UUID,
        lease_id: UUID,
        state: NotificationState,
        code: str,
        now: datetime,
    ) -> bool: ...


class NotificationRepository(
    NotificationPreferencesRepository,
    NotificationDeliveryRepository,
    Protocol,
):
    pass
