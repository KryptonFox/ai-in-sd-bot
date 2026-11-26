import asyncio
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.clock import FrozenClock
from app.core.trace import OperationTrace
from app.db.notification_records import (
    CityRecord,
    CitySelectionRecord,
    CitySelectionState,
    NotificationKind,
    NotificationSettingsRecord,
    NotificationState,
    OutgoingNotificationRecord,
)
from app.services.notifications.contracts import (
    BotDelivery,
    BriefingContent,
    BriefingContext,
    BriefingSection,
    CitySelection,
    NotificationDeliveryError,
    NotificationFailure,
    NotificationPolicies,
    TickReport,
    TraceSink,
)
from app.services.notifications.geocoding import HttpCityResolver
from app.services.notifications.service import NotificationService
from app.services.notifications.windows import (
    briefing_scheduled_at,
    briefing_window_open,
    first_valid_local_moment,
)

MOSCOW = ZoneInfo("Europe/Moscow")
CITY = CityRecord(
    name="Казань",
    country="Россия",
    region="Татарстан",
    latitude=55.7963,
    longitude=49.1088,
)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeCityResolver:
    def __init__(self, candidates: tuple[CityRecord, ...] = (CITY,)) -> None:
        self.candidates = candidates
        self.queries: list[str] = []

    async def resolve(self, query: str) -> tuple[CityRecord, ...]:
        self.queries.append(query)
        return self.candidates


class MemoryNotificationRepository:
    def __init__(self) -> None:
        self.settings_by_user: dict[int, NotificationSettingsRecord] = {}
        self.selections: dict[uuid.UUID, CitySelectionRecord] = {}
        self.events: dict[uuid.UUID, OutgoingNotificationRecord] = {}
        self.prepared_reminders = 0
        self.recovered_leases = 0

    def add_settings(
        self,
        user_id: int,
        *,
        destination_chat_id: int | None = 100,
        timezone: str | None = "Europe/Moscow",
        city: CityRecord | None = CITY,
        briefing_enabled: bool = True,
        now: datetime | None = None,
    ) -> NotificationSettingsRecord:
        moment = now or datetime(2031, 5, 1, tzinfo=UTC)
        settings = NotificationSettingsRecord(
            user_id=user_id,
            destination_chat_id=destination_chat_id,
            city=city,
            city_confirmed_at=moment if city is not None else None,
            briefing_enabled=briefing_enabled,
            briefing_consent_at=moment if briefing_enabled else None,
            timezone=timezone,
            updated_at=moment,
        )
        self.settings_by_user[user_id] = settings
        return settings

    def add_event(self, job: OutgoingNotificationRecord) -> None:
        self.events[job.id] = job

    async def remember_private_chat(self, user_id: int, chat_id: int, now: datetime) -> None:
        current = self.settings_by_user.get(user_id)
        if current is None:
            current = NotificationSettingsRecord(
                user_id=user_id,
                destination_chat_id=None,
                city=None,
                city_confirmed_at=None,
                briefing_enabled=False,
                briefing_consent_at=None,
                timezone=None,
                updated_at=now,
            )
        self.settings_by_user[user_id] = replace(
            current,
            destination_chat_id=chat_id,
            updated_at=now,
        )

    async def settings(self, user_id: int) -> NotificationSettingsRecord | None:
        return self.settings_by_user.get(user_id)

    async def create_city_selection(self, selection: CitySelectionRecord) -> None:
        for existing_id, existing in list(self.selections.items()):
            if (
                existing.user_id == selection.user_id
                and existing.state is CitySelectionState.PENDING
            ):
                self.selections[existing_id] = replace(
                    existing,
                    state=CitySelectionState.CANCELLED,
                )
        self.selections[selection.id] = selection

    async def city_selection(self, selection_id: uuid.UUID) -> CitySelectionRecord | None:
        return self.selections.get(selection_id)

    async def confirm_city_selection(
        self,
        selection_id: uuid.UUID,
        user_id: int,
        index: int,
        now: datetime,
    ) -> NotificationSettingsRecord | None:
        selection = self.selections.get(selection_id)
        if selection is None or selection.user_id != user_id:
            return None
        if selection.state is not CitySelectionState.PENDING:
            return None
        if selection.expires_at <= now:
            self.selections[selection_id] = replace(
                selection,
                state=CitySelectionState.EXPIRED,
            )
            return None
        if index < 0 or index >= len(selection.candidates):
            return None

        city = selection.candidates[index]
        self.selections[selection_id] = replace(
            selection,
            state=CitySelectionState.CONFIRMED,
            selected_index=index,
        )
        current = self.settings_by_user.get(user_id)
        if current is None:
            current = NotificationSettingsRecord(
                user_id=user_id,
                destination_chat_id=None,
                city=None,
                city_confirmed_at=None,
                briefing_enabled=False,
                briefing_consent_at=None,
                timezone=None,
                updated_at=now,
            )
        updated = replace(
            current,
            city=city,
            city_confirmed_at=now,
            updated_at=now,
        )
        self.settings_by_user[user_id] = updated
        return updated

    async def cancel_city_selection(
        self,
        selection_id: uuid.UUID,
        user_id: int,
        now: datetime,
    ) -> bool:
        selection = self.selections.get(selection_id)
        if (
            selection is None
            or selection.user_id != user_id
            or selection.state is not CitySelectionState.PENDING
        ):
            return False
        self.selections[selection_id] = replace(
            selection,
            state=CitySelectionState.CANCELLED,
        )
        return True

    async def set_briefing_enabled(
        self,
        user_id: int,
        enabled: bool,
        now: datetime,
    ) -> NotificationSettingsRecord | None:
        current = self.settings_by_user.get(user_id)
        if current is None:
            return None
        if enabled and (
            current.destination_chat_id is None
            or current.city is None
            or current.city_confirmed_at is None
        ):
            return None
        updated = replace(
            current,
            briefing_enabled=enabled,
            briefing_consent_at=now if enabled else None,
            updated_at=now,
        )
        self.settings_by_user[user_id] = updated
        return updated

    async def prepare_reminder_events(self, now: datetime, grace: timedelta) -> int:
        return self.prepared_reminders

    async def briefing_candidates(self) -> list[NotificationSettingsRecord]:
        return [
            settings
            for settings in self.settings_by_user.values()
            if settings.briefing_enabled
            and settings.destination_chat_id is not None
            and settings.city is not None
            and settings.timezone is not None
        ]

    async def create_briefing_event(
        self,
        user_id: int,
        local_date: date,
        timezone: str,
        scheduled_for: datetime,
        trace_id: uuid.UUID,
        now: datetime,
    ) -> bool:
        event_key = f"briefing:{user_id}:{local_date.isoformat()}"
        if any(event.event_key == event_key for event in self.events.values()):
            return False
        settings = self.settings_by_user.get(user_id)
        if settings is None or not settings.briefing_enabled:
            return False
        job = OutgoingNotificationRecord(
            id=uuid.uuid4(),
            event_key=event_key,
            user_id=user_id,
            kind=NotificationKind.DAILY_BRIEFING,
            destination_chat_id=settings.destination_chat_id,
            payload={
                "local_date": local_date.isoformat(),
                "timezone": timezone,
                "city_name": settings.city.name if settings.city else None,
                "city_country": settings.city.country if settings.city else None,
                "city_region": settings.city.region if settings.city else None,
                "city_latitude": settings.city.latitude if settings.city else None,
                "city_longitude": settings.city.longitude if settings.city else None,
            },
            state=NotificationState.PENDING,
            attempt_count=0,
            scheduled_for=scheduled_for,
            next_attempt_at=now,
            lease_id=uuid.uuid4(),
            trace_id=trace_id,
        )
        self.events[job.id] = job
        return True

    async def recover_expired_leases(self, now: datetime, max_attempts: int) -> int:
        return self.recovered_leases

    async def claim_due_event(
        self,
        now: datetime,
        lease_until: datetime,
        max_attempts: int,
    ) -> OutgoingNotificationRecord | None:
        due = [
            job
            for job in self.events.values()
            if job.state in {NotificationState.PENDING, NotificationState.RETRY}
            and job.next_attempt_at <= now
            and job.attempt_count < max_attempts
        ]
        if not due:
            return None

        job = min(due, key=lambda item: (item.next_attempt_at, item.event_key))
        claimed = replace(
            job,
            state=NotificationState.SENDING,
            attempt_count=job.attempt_count + 1,
            lease_id=uuid.uuid4(),
            lease_until=lease_until,
        )
        self.events[job.id] = claimed
        return claimed

    async def save_payload(
        self,
        event_id: uuid.UUID,
        lease_id: uuid.UUID,
        payload: dict[str, object],
        now: datetime,
    ) -> bool:
        job = self.events.get(event_id)
        if job is None or job.lease_id != lease_id or job.state is not NotificationState.SENDING:
            return False
        self.events[event_id] = replace(job, payload=dict(payload))
        return True

    async def mark_sent(self, event_id: uuid.UUID, lease_id: uuid.UUID, now: datetime) -> bool:
        job = self.events.get(event_id)
        if job is None or job.lease_id != lease_id or job.state is not NotificationState.SENDING:
            return False
        self.events[event_id] = replace(job, state=NotificationState.SENT)
        return True

    async def mark_retry(
        self,
        event_id: uuid.UUID,
        lease_id: uuid.UUID,
        next_attempt_at: datetime,
        code: str,
        now: datetime,
    ) -> bool:
        job = self.events.get(event_id)
        if job is None or job.lease_id != lease_id or job.state is not NotificationState.SENDING:
            return False
        self.events[event_id] = replace(
            job,
            state=NotificationState.RETRY,
            next_attempt_at=next_attempt_at,
        )
        return True

    async def mark_terminal(
        self,
        event_id: uuid.UUID,
        lease_id: uuid.UUID,
        state: NotificationState,
        code: str,
        now: datetime,
    ) -> bool:
        job = self.events.get(event_id)
        if job is None or job.lease_id != lease_id or job.state is not NotificationState.SENDING:
            return False
        self.events[event_id] = replace(job, state=state)
        return True


class DeliveryRecorder:
    def __init__(self, errors: list[Exception | None] | None = None) -> None:
        self.errors = list(errors or [])
        self.messages: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self.messages.append((chat_id, text))


class TraceCollector:
    def __init__(self) -> None:
        self.traces: list[OperationTrace] = []

    async def __call__(self, trace: OperationTrace) -> None:
        self.traces.append(trace)


def reminder_job(
    *,
    user_id: int = 42,
    scheduled_for: datetime,
    attempt_count: int = 0,
    state: NotificationState = NotificationState.PENDING,
    text: str = "Сдать задание",
) -> OutgoingNotificationRecord:
    return OutgoingNotificationRecord(
        id=uuid.uuid4(),
        event_key=f"reminder:{uuid.uuid4().int}",
        user_id=user_id,
        kind=NotificationKind.REMINDER,
        destination_chat_id=100,
        payload={"text": text, "timezone": "Europe/Moscow"},
        state=state,
        attempt_count=attempt_count,
        scheduled_for=scheduled_for,
        next_attempt_at=scheduled_for,
        lease_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
    )


def briefing_job(
    *,
    user_id: int = 42,
    local_date: date = date(2031, 5, 1),
    scheduled_for: datetime = datetime(2031, 5, 1, 5, 0, tzinfo=UTC),
    state: NotificationState = NotificationState.PENDING,
) -> OutgoingNotificationRecord:
    return OutgoingNotificationRecord(
        id=uuid.uuid4(),
        event_key=f"briefing:{user_id}:{local_date.isoformat()}",
        user_id=user_id,
        kind=NotificationKind.DAILY_BRIEFING,
        destination_chat_id=100,
        payload={
            "local_date": local_date.isoformat(),
            "timezone": "Europe/Moscow",
            "city_name": CITY.name,
            "city_country": CITY.country,
            "city_region": CITY.region,
            "city_latitude": CITY.latitude,
            "city_longitude": CITY.longitude,
        },
        state=state,
        attempt_count=0,
        scheduled_for=scheduled_for,
        next_attempt_at=scheduled_for,
        lease_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
    )


def make_service(
    repository: MemoryNotificationRepository,
    clock: FrozenClock | MutableClock,
    *,
    deliver: BotDelivery | None = None,
    briefing=None,
    trace_sink: TraceSink | None = None,
    policies: NotificationPolicies | None = None,
) -> NotificationService:
    async def default_briefing(context: BriefingContext) -> BriefingContent:
        return BriefingContent(
            sections=(BriefingSection(title="Занятия", text="Математика, 10:00"),)
        )

    return NotificationService(
        repository,
        clock=clock,
        deliver=deliver or DeliveryRecorder(),
        briefing=briefing or default_briefing,
        city_resolver=FakeCityResolver(),
        policies=policies,
        trace_sink=trace_sink,
    )


def test_briefing_window_includes_boundaries() -> None:
    start = time(8, 0)
    end = time(9, 0)

    assert briefing_window_open(datetime(2031, 5, 1, 8, 0), start, end)
    assert briefing_window_open(datetime(2031, 5, 1, 9, 0), start, end)
    assert not briefing_window_open(datetime(2031, 5, 1, 9, 0, 1), start, end)
    assert not briefing_window_open(datetime(2031, 5, 1, 7, 59, 59), start, end)


def test_first_valid_local_moment_uses_earliest_ambiguous_fold() -> None:
    new_york = ZoneInfo("America/New_York")
    ambiguous = datetime(2026, 11, 1, 1, 30)
    first = first_valid_local_moment(ambiguous, new_york)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1).astimezone(UTC)

    assert first < second
    assert first.astimezone(new_york).replace(tzinfo=None) == ambiguous


def test_first_valid_local_moment_skips_dst_gap() -> None:
    new_york = ZoneInfo("America/New_York")
    nonexistent = datetime(2026, 3, 8, 2, 30)
    valid = first_valid_local_moment(nonexistent, new_york)

    assert valid.astimezone(new_york).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 0)
    assert briefing_scheduled_at(date(2026, 3, 8), new_york, time(2, 30)) == valid


async def test_reminder_delivery_marks_late_attempt() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now - timedelta(minutes=5)))
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.sent == 1
    assert len(deliver.messages) == 1
    chat_id, text = deliver.messages[0]
    assert chat_id == 100
    assert "задержка 5 мин." in text
    assert "Сдать задание" in text


async def test_reminder_grace_boundary_is_inclusive() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(
        reminder_job(
            scheduled_for=now - timedelta(minutes=15),
            text="Ровно пятнадцать",
        )
    )
    repository.add_event(
        reminder_job(
            scheduled_for=now - timedelta(minutes=15, seconds=1),
            text="Слишком старое",
        )
    )
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.sent == 1
    assert report.missed == 1
    assert len(deliver.messages) == 1
    assert "Ровно пятнадцать" in deliver.messages[0][1]
    states = sorted(job.state.value for job in repository.events.values())
    assert states == ["missed", "sent"]


async def test_future_reminder_is_not_claimed() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now + timedelta(minutes=1)))
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.sent == 0
    assert deliver.messages == []


async def test_briefing_prepares_once_and_renders_partial_sections() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    trace_sink = TraceCollector()

    async def briefing(context: BriefingContext) -> BriefingContent:
        assert context.local_date == date(2031, 5, 1)
        assert context.timezone == "Europe/Moscow"
        assert context.city.name == "Казань"
        return BriefingContent(
            sections=(
                BriefingSection(title="Занятия", text="Математика, 10:00"),
                BriefingSection(title="Погода", problem="прогноз недоступен"),
            )
        )

    deliver = DeliveryRecorder()
    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        briefing=briefing,
        trace_sink=trace_sink,
    )

    first = await service.tick()
    second = await service.tick()

    assert first.prepared_briefings == 1
    assert first.sent == 1
    assert second.prepared_briefings == 0
    assert second.sent == 0
    assert len(deliver.messages) == 1
    text = deliver.messages[0][1]
    assert "Сводка на 01.05.2031" in text
    assert "Математика, 10:00" in text
    assert "⚠️ прогноз недоступен" in text
    assert "Подготовлено в 08:15 (Europe/Moscow)." in text
    assert len(trace_sink.traces) == 1
    trace = trace_sink.traces[0]
    assert trace.route == "daily_briefing"
    assert [step.component for step in trace.steps] == [
        "notification",
        "briefing",
        "telegram",
    ]


async def test_briefing_is_not_caught_up_after_daily_window() -> None:
    now = datetime(2031, 5, 1, 7, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.prepared_briefings == 0
    assert report.sent == 0
    assert repository.events == {}
    assert deliver.messages == []


async def test_briefing_after_window_is_missed() -> None:
    now = datetime(2031, 5, 1, 6, 1, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.missed == 1
    assert deliver.messages == []


async def test_briefing_disabled_before_send_is_suppressed() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    settings = repository.add_settings(42)
    repository.settings_by_user[42] = replace(
        settings,
        briefing_enabled=False,
        briefing_consent_at=None,
    )
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.suppressed == 1
    assert deliver.messages == []


async def test_missing_destination_suppresses_reminder() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42, destination_chat_id=None)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder()
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.suppressed == 1
    assert deliver.messages == []


async def test_trace_sink_failure_does_not_block_delivery() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder()

    async def failing_sink(trace: OperationTrace) -> None:
        raise RuntimeError("trace storage down")

    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        trace_sink=failing_sink,
    )

    report = await service.tick()

    assert report.sent == 1
    assert len(deliver.messages) == 1


async def test_briefing_disabled_during_preparation_is_not_sent() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()

    async def briefing(context: BriefingContext) -> BriefingContent:
        settings = repository.settings_by_user[42]
        repository.settings_by_user[42] = replace(
            settings,
            briefing_enabled=False,
            briefing_consent_at=None,
        )
        return BriefingContent((BriefingSection("Занятия", text="Математика"),))

    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        briefing=briefing,
    )

    report = await service.tick()

    assert report.suppressed == 1
    assert deliver.messages == []
    assert next(iter(repository.events.values())).state is NotificationState.SUPPRESSED


class SettingsRecheckRepository(MemoryNotificationRepository):
    def __init__(self, changes: list[NotificationSettingsRecord | None]) -> None:
        super().__init__()
        self._changes = list(changes)
        self.settings_calls = 0

    async def settings(self, user_id: int) -> NotificationSettingsRecord | None:
        self.settings_calls += 1
        if self.settings_calls > 1 and self._changes:
            change = self._changes.pop(0)
            if change is not None:
                self.settings_by_user[user_id] = change
        return await super().settings(user_id)


async def test_destination_is_reread_immediately_before_send() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = SettingsRecheckRepository([])
    settings = repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder()
    repository._changes.append(replace(settings, destination_chat_id=200))
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.sent == 1
    assert deliver.messages[0][0] == 200
    assert repository.settings_calls >= 2


async def test_destination_removed_during_preparation_suppresses_delivery() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = SettingsRecheckRepository([])
    settings = repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder()
    repository._changes.append(replace(settings, destination_chat_id=None))
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.suppressed == 1
    assert deliver.messages == []
    assert repository.settings_calls >= 2


async def test_briefing_city_change_during_preparation_invalidates_stale_content() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()
    another_city = CityRecord(
        name="Москва",
        country="Россия",
        region=None,
        latitude=55.7558,
        longitude=37.6173,
    )

    async def briefing(context: BriefingContext) -> BriefingContent:
        settings = repository.settings_by_user[42]
        repository.settings_by_user[42] = replace(
            settings,
            city=another_city,
            city_confirmed_at=now,
        )
        return BriefingContent((BriefingSection("Погода", text="старые данные"),))

    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        briefing=briefing,
    )

    report = await service.tick()

    assert report.suppressed == 1
    assert deliver.messages == []
    assert next(iter(repository.events.values())).state is NotificationState.SUPPRESSED


async def test_briefing_crossing_window_during_preparation_is_missed() -> None:
    now = datetime(2031, 5, 1, 5, 59, 50, tzinfo=UTC)
    clock = MutableClock(now)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()

    async def briefing(context: BriefingContext) -> BriefingContent:
        clock.advance(timedelta(seconds=20))
        return BriefingContent((BriefingSection("Занятия", text="Математика"),))

    service = make_service(
        repository,
        clock,
        deliver=deliver,
        briefing=briefing,
    )

    report = await service.tick()

    assert report.missed == 1
    assert deliver.messages == []
    assert next(iter(repository.events.values())).state is NotificationState.MISSED


async def test_briefing_preparation_beyond_lease_does_not_send_stale_content() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    clock = MutableClock(now)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()

    async def briefing(context: BriefingContext) -> BriefingContent:
        clock.advance(timedelta(seconds=31))
        return BriefingContent((BriefingSection("Занятия", text="Математика"),))

    service = make_service(
        repository,
        clock,
        deliver=deliver,
        briefing=briefing,
        policies=NotificationPolicies(lease_duration=timedelta(seconds=30)),
    )

    report = await service.tick()

    assert report.failed == 1
    assert deliver.messages == []
    assert next(iter(repository.events.values())).state is NotificationState.SENDING


async def test_transient_error_retries_with_delay_then_sends() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder(
        errors=[NotificationDeliveryError("telegram_network", retryable=True)]
    )
    service = make_service(repository, clock, deliver=deliver)

    first = await service.tick()
    job = next(iter(repository.events.values()))
    assert first.retried == 1
    assert job.state is NotificationState.RETRY
    assert job.next_attempt_at == now + timedelta(seconds=30)

    clock.advance(timedelta(seconds=30))
    second = await service.tick()

    assert second.sent == 1
    assert len(deliver.messages) == 1
    assert next(iter(repository.events.values())).state is NotificationState.SENT


async def test_retry_delay_starts_after_slow_telegram_attempt() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    clock = MutableClock(now)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))

    async def deliver(chat_id: int, text: str) -> None:
        clock.advance(timedelta(seconds=40))
        raise NotificationDeliveryError("telegram_timeout", retryable=True)

    service = make_service(repository, clock, deliver=deliver)

    first = await service.tick()

    job = next(iter(repository.events.values()))
    assert first.retried == 1
    assert job.state is NotificationState.RETRY
    assert job.next_attempt_at == now + timedelta(seconds=70)


async def test_permanent_error_fails_without_retry() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder(
        errors=[NotificationDeliveryError("telegram_api_error", retryable=False)]
    )
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.failed == 1
    assert next(iter(repository.events.values())).state is NotificationState.FAILED
    assert deliver.messages == []


async def test_third_failed_attempt_ends_job() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now, attempt_count=2))
    deliver = DeliveryRecorder(
        errors=[NotificationDeliveryError("telegram_network", retryable=True)]
    )
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    report = await service.tick()

    assert report.failed == 1
    assert next(iter(repository.events.values())).state is NotificationState.FAILED


async def test_retry_outside_window_becomes_missed() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now - timedelta(minutes=5)))
    deliver = DeliveryRecorder(
        errors=[NotificationDeliveryError("telegram_network", retryable=True)]
    )
    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        policies=NotificationPolicies(reminder_grace=timedelta(minutes=5)),
    )

    report = await service.tick()

    assert report.missed == 1
    assert next(iter(repository.events.values())).state is NotificationState.MISSED


async def test_telegram_retry_after_extends_delay() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(reminder_job(scheduled_for=now))
    deliver = DeliveryRecorder(
        errors=[
            NotificationDeliveryError(
                "telegram_retry_after",
                retryable=True,
                retry_after=45,
            )
        ]
    )
    service = make_service(repository, FrozenClock(now), deliver=deliver)

    await service.tick()

    job = next(iter(repository.events.values()))
    assert job.next_attempt_at == now + timedelta(seconds=45)


async def test_briefing_failure_sends_fallback_explaining_gap() -> None:
    now = datetime(2031, 5, 1, 5, 15, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    repository.add_event(briefing_job())
    deliver = DeliveryRecorder()

    async def failing_briefing(context: BriefingContext) -> BriefingContent:
        raise RuntimeError("source failed")

    service = make_service(
        repository,
        FrozenClock(now),
        deliver=deliver,
        briefing=failing_briefing,
    )

    report = await service.tick()

    assert report.sent == 1
    assert "Источники сводки временно недоступны." in deliver.messages[0][1]


async def test_city_selection_returns_confirmation_candidates() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    resolver = FakeCityResolver()

    async def no_briefing(context: BriefingContext) -> BriefingContent:
        return BriefingContent()

    service = NotificationService(
        repository,
        clock=FrozenClock(now),
        deliver=DeliveryRecorder(),
        briefing=no_briefing,
        city_resolver=resolver,
        policies=NotificationPolicies(city_selection_ttl=timedelta(minutes=5)),
    )

    selection = await service.prepare_city_selection(42, "  Казань, Россия  ")

    assert selection.candidates == (CITY,)
    assert resolver.queries == ["Казань, Россия"]
    assert selection.expires_at == now + timedelta(minutes=5)
    assert await repository.city_selection(selection.id) is not None


async def test_city_selection_rejects_expired_and_foreign_callbacks() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42)
    service = make_service(repository, FrozenClock(now))
    selection = await service.prepare_city_selection(42, "Казань")

    with pytest.raises(NotificationFailure, match="недоступен"):
        await service.confirm_city_selection(43, selection.id, 0)

    expired = replace(
        repository.selections[selection.id],
        expires_at=now - timedelta(seconds=1),
    )
    repository.selections[selection.id] = expired
    with pytest.raises(NotificationFailure, match="недоступен"):
        await service.confirm_city_selection(42, selection.id, 0)


async def test_enable_briefing_requires_city_and_timezone() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42, city=None, briefing_enabled=False, timezone="Europe/Moscow")
    service = make_service(repository, FrozenClock(now))

    with pytest.raises(NotificationFailure) as missing_city:
        await service.enable_briefing(42)
    assert missing_city.value.code == "city_required"

    repository.add_settings(42, city=CITY, briefing_enabled=False, timezone=None)
    with pytest.raises(NotificationFailure) as missing_timezone:
        await service.enable_briefing(42)
    assert missing_timezone.value.code == "timezone_required"

    settings = repository.add_settings(
        42,
        city=CITY,
        briefing_enabled=False,
        timezone="Europe/Moscow",
    )
    assert settings.destination_chat_id == 100
    view = await service.enable_briefing(42)
    assert view.briefing_enabled


async def test_tick_report_defaults_and_city_selection_view() -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    repository.add_settings(42, city=None, briefing_enabled=False)
    service = make_service(repository, FrozenClock(now))

    view = await service.notification_settings(42)
    report = await service.tick()

    assert view.destination_saved
    assert view.city is None
    assert report == TickReport()
    assert asdict(report) == {
        "prepared_reminders": 0,
        "prepared_briefings": 0,
        "recovered_leases": 0,
        "sent": 0,
        "retried": 0,
        "failed": 0,
        "missed": 0,
        "suppressed": 0,
    }


async def test_run_survives_tick_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
    repository = MemoryNotificationRepository()
    service = make_service(repository, FrozenClock(now))
    calls = 0

    async def failing_tick() -> TickReport:
        nonlocal calls
        calls += 1
        raise RuntimeError("tick failed")

    async def stop_after_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "tick", failing_tick)
    monkeypatch.setattr(asyncio, "sleep", stop_after_sleep)

    with pytest.raises(asyncio.CancelledError):
        await service.run()

    assert calls == 1


def test_city_selection_view_is_public_without_internal_ids() -> None:
    selection = CitySelection(
        id=uuid.uuid4(),
        candidates=(CITY,),
        expires_at=datetime(2031, 5, 1, tzinfo=UTC),
    )

    assert selection.id is not None
    assert selection.candidates[0].region == "Татарстан"


async def test_http_city_resolver_uses_shared_client_and_existing_geocoder() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "results": [
                        {
                            "id": 1,
                            "name": "Казань",
                            "country": "Россия",
                            "country_code": "RU",
                            "admin1": "Татарстан",
                            "population": 1_200_000,
                            "latitude": 55.7963,
                            "longitude": 49.1088,
                        }
                    ]
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HttpCityResolver(client)
        candidates = await resolver.resolve("Казань, Россия")

    assert candidates == (CITY,)
    assert len(requests) == 1
    assert requests[0].url.host == "geocoding-api.open-meteo.com"
