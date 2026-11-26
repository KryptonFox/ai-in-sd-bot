import io
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Document, Message, User

from app.core.clock import FrozenClock
from app.core.schemas.calendar import (
    CalendarImportError,
    CalendarImportErrorCode,
    CalendarImportSummary,
    CalendarSnapshot,
    StoredCalendarEvent,
)
from app.core.schemas.tools import ToolFailure
from app.db.repositories import calendar_repo
from app.handlers.calendar_import import (
    BoundedBytesIO,
    CalendarFileTooLarge,
    ICalendarDocumentFilter,
    handle_calendar_document,
)
from app.services import calendar_import as calendar_import_module
from app.services.calendar_import import CalendarImportService, is_ics_filename
from app.services.calendar_parser import parse_calendar

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
MOSCOW = "Europe/Moscow"

CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/Moscow:20260105T100000
DTEND;TZID=Europe/Moscow:20260105T113000
SUMMARY:Математика
LOCATION:Аудитория 1
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4
EXDATE;TZID=Europe/Moscow:20260112T100000
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=Europe/Moscow:20260119T100000
DTSTART;TZID=Europe/Moscow:20260119T120000
DTEND;TZID=Europe/Moscow:20260119T133000
SUMMARY:Перенесённая
END:VEVENT
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20260107
SUMMARY:День рождения
END:VEVENT
BEGIN:VEVENT
UID:floating
DTSTART:20260106T090000
DTEND:20260106T100000
SUMMARY:Без зоны
END:VEVENT
END:VCALENDAR
""".encode()

SMALL_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:single
DTSTART;TZID=Europe/Moscow:20260202T100000
DTEND;TZID=Europe/Moscow:20260202T110000
SUMMARY:Лекция
END:VEVENT
END:VCALENDAR
""".encode()
CANCELLED_OCCURRENCE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly
DTSTART;TZID=Europe/Moscow:20260105T100000
DTEND;TZID=Europe/Moscow:20260105T113000
SUMMARY:Математика
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=Europe/Moscow:20260112T100000
DTSTART;TZID=Europe/Moscow:20260112T100000
DTEND;TZID=Europe/Moscow:20260112T113000
STATUS:CANCELLED
SUMMARY:Математика
END:VEVENT
END:VCALENDAR
""".encode()


def snapshot(events: tuple[StoredCalendarEvent, ...] = ()) -> CalendarSnapshot:
    return CalendarSnapshot(
        user_id=1,
        timezone=MOSCOW,
        source_name="schedule.ics",
        imported_at=NOW,
        events=events,
    )


def test_parse_timed_all_day_floating_and_timezone_events() -> None:
    events = parse_calendar(CALENDAR, MOSCOW)
    by_uid = {event.uid: event for event in events}
    weekly = next(event for event in events if event.uid == "weekly" and not event.recurrence_id)

    assert set(by_uid) == {"weekly", "all-day", "floating"}
    assert weekly.timezone == MOSCOW
    assert weekly.starts_at.hour == 10
    assert weekly.recurrence_id == ""
    assert by_uid["all-day"].all_day is True
    assert by_uid["all-day"].starts_at.date().isoformat() == "2026-01-07"
    assert by_uid["all-day"].starts_at.tzinfo is not None
    assert by_uid["floating"].timezone == MOSCOW
    assert by_uid["floating"].starts_at.hour == 9
    assert by_uid["floating"].starts_at.utcoffset() == timedelta(hours=3)


def test_parse_recurrence_exceptions_exdates_and_moved_occurrence() -> None:
    events = parse_calendar(CALENDAR, MOSCOW)
    weekly = [event for event in events if event.uid == "weekly"]
    assert len(weekly) == 2
    master = next(event for event in weekly if not event.recurrence_id)
    moved = next(event for event in weekly if event.recurrence_id)
    assert master.rrule == "FREQ=WEEKLY;COUNT=4;BYDAY=MO"
    assert [value.isoformat() for value in master.exdate] == ["2026-01-12T10:00:00+03:00"]
    assert moved.recurrence_id == "2026-01-19T07:00:00+00:00"

    calendar = snapshot(tuple(events))
    first = calendar.day(datetime(2026, 1, 5).date())
    skipped = calendar.day(datetime(2026, 1, 12).date())
    moved_day = calendar.day(datetime(2026, 1, 19).date())

    assert first.lessons[0].title == "Математика"
    assert skipped.is_free_day is True
    assert moved_day.lessons[0].title == "Перенесённая"
    assert (moved_day.lessons[0].start, moved_day.lessons[0].end) == ("12:00", "13:30")


def test_cancelled_occurrence_removes_only_its_recurrence() -> None:
    events = parse_calendar(CANCELLED_OCCURRENCE, MOSCOW)
    calendar = snapshot(tuple(events))

    first = calendar.day(date(2026, 1, 5))
    cancelled = calendar.day(date(2026, 1, 12))

    assert first.lessons[0].title == "Математика"
    assert cancelled.lessons == []
    assert cancelled.is_free_day is True
    assert len(events) == 2


def test_parse_all_day_event_uses_full_day_and_user_timezone() -> None:
    calendar = snapshot(tuple(parse_calendar(CALENDAR, MOSCOW)))
    day = calendar.day(datetime(2026, 1, 7).date())

    assert day.data_available is True
    assert day.is_free_day is False
    assert day.lessons[0].start == "00:00"
    assert day.lessons[0].end == "23:59"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not a calendar",
        b"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:x\n"
        b"DTSTART;TZID=Custom/Unknown:20260101T100000\nEND:VEVENT\nEND:VCALENDAR\n",
        b"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:x\n"
        b"DTSTART:20260101T100000Z\nDTEND:20260101T110000Z\n"
        b"RRULE:FREQ=HOURLY;COUNT=3\nEND:VEVENT\nEND:VCALENDAR\n",
    ],
)
def test_parse_rejects_corrupt_and_unsupported_calendars(payload: bytes) -> None:
    with pytest.raises(CalendarImportError):
        parse_calendar(payload, MOSCOW)


def test_parse_rejects_recurrence_range_and_orphan_exception() -> None:
    ranged = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
DTSTART:20260101T100000Z
RECURRENCE-ID;RANGE=THISANDFUTURE:20260101T100000Z
SUMMARY:X
END:VEVENT
END:VCALENDAR
"""
    orphan = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
RECURRENCE-ID:20260101T100000Z
DTSTART:20260101T120000Z
SUMMARY:X
END:VEVENT
END:VCALENDAR
"""

    with pytest.raises(CalendarImportError):
        parse_calendar(ranged, MOSCOW)
    with pytest.raises(CalendarImportError):
        parse_calendar(orphan, MOSCOW)


def test_distant_daily_recurrence_stops_after_work_budget() -> None:
    event = StoredCalendarEvent(
        uid="ancient",
        summary="Daily",
        starts_at=datetime(1, 1, 1, tzinfo=UTC),
        ends_at=datetime(1, 1, 1, 1, tzinfo=UTC),
        timezone="UTC",
        rrule="FREQ=DAILY",
    )

    with pytest.raises(ToolFailure) as error:
        snapshot((event,)).day(date(2026, 1, 5))

    assert error.value.code == "schedule_unavailable"


def test_filename_check_is_case_insensitive_and_strict() -> None:
    assert is_ics_filename("Schedule.ICS") is True
    assert is_ics_filename("calendar.ics.txt") is False
    assert is_ics_filename(None) is False


async def test_import_service_replaces_snapshot_and_keeps_previous_on_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[int, CalendarSnapshot] = {}

    async def get_timezone(pool: object, user_id: int) -> str:
        return MOSCOW

    async def replace(pool: object, value: CalendarSnapshot) -> bool:
        replaced = value.user_id in store
        store[value.user_id] = value
        return replaced

    monkeypatch.setattr(calendar_import_module, "get_timezone", get_timezone)
    monkeypatch.setattr(calendar_import_module, "replace_calendar", replace)
    service = CalendarImportService(MagicMock(), FrozenClock(NOW))

    first = await service.import_calendar(1, "schedule.ics", SMALL_CALENDAR)
    assert first.event_count == 1
    assert first.replaced is False

    second = await service.import_calendar(1, "schedule.ics", CALENDAR)
    assert second.event_count == 4
    assert second.replaced is True
    assert len(store[1].events) == 4

    with pytest.raises(CalendarImportError):
        await service.import_calendar(1, "schedule.ics", b"broken")
    assert len(store[1].events) == 4


async def test_import_service_rejects_non_ics_name_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace = AsyncMock(return_value=False)
    monkeypatch.setattr(calendar_import_module, "replace_calendar", replace)
    service = CalendarImportService(MagicMock(), FrozenClock(NOW))

    with pytest.raises(CalendarImportError) as error:
        await service.import_calendar(1, "schedule.txt", SMALL_CALENDAR)

    assert error.value.code is CalendarImportErrorCode.INVALID_CALENDAR
    replace.assert_not_awaited()


class RecordingConnection:
    def __init__(self, calendar_row: object = None, event_rows: list[object] | None = None):
        self.calendar_row = calendar_row
        self.event_rows = event_rows or []
        self.calls: list[tuple[Any, ...]] = []

    def transaction(self, *args: object, **kwargs: object) -> Any:
        self.calls.append(("transaction", args, kwargs))

        class Context:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc_info: object) -> bool:
                return False

        return Context()

    async def fetchval(self, query: str, *args: object) -> bool:
        self.calls.append(("fetchval", query, args))
        return True

    async def execute(self, query: str, *args: object) -> None:
        self.calls.append(("execute", query, args))

    async def executemany(self, query: str, rows: list[object]) -> None:
        self.calls.append(("executemany", query, rows))

    async def fetchrow(self, query: str, *args: object) -> object:
        self.calls.append(("fetchrow", query, args))
        return self.calendar_row

    async def fetch(self, query: str, *args: object) -> list[object]:
        self.calls.append(("fetch", query, args))
        return self.event_rows


class RecordingPool:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> Any:
        connection = self.connection

        class Context:
            async def __aenter__(self) -> Any:
                return connection

            async def __aexit__(self, *exc_info: object) -> bool:
                return False

        return Context()


async def test_repository_replace_is_single_transaction_for_one_owner() -> None:
    connection = RecordingConnection()
    pool = cast(asyncpg.Pool, RecordingPool(connection))
    event = StoredCalendarEvent(
        uid="one",
        summary="Лекция",
        starts_at=datetime(2026, 2, 2, 10, tzinfo=UTC),
        ends_at=datetime(2026, 2, 2, 11, tzinfo=UTC),
        timezone="UTC",
    )
    value = snapshot((event,))

    replaced = await calendar_repo.replace_calendar(pool, value)

    assert replaced is True
    kinds = [call[0] for call in connection.calls]
    assert kinds[0] == "transaction"
    assert "INSERT INTO user_settings" in str(connection.calls[1][1])
    assert "DELETE FROM personal_calendar_events" in str(connection.calls[4][1])
    insert_call = next(call for call in connection.calls if call[0] == "executemany")
    assert insert_call[2][0][0] == 1


async def test_repository_load_snapshot_is_owner_scoped() -> None:
    row = {
        "user_id": 7,
        "timezone": MOSCOW,
        "source_name": "schedule.ics",
        "imported_at": NOW,
    }
    event_row = {
        "uid": "one",
        "recurrence_id": "",
        "summary": "Лекция",
        "location": "",
        "starts_at": datetime(2026, 2, 2, 7, tzinfo=UTC),
        "ends_at": datetime(2026, 2, 2, 8, tzinfo=UTC),
        "all_day": False,
        "cancelled": False,
        "timezone": MOSCOW,
        "rrule": None,
        "rdate": [],
        "exdate": [],
    }
    connection = RecordingConnection(calendar_row=row, event_rows=[event_row])
    pool = cast(asyncpg.Pool, RecordingPool(connection))

    loaded = await calendar_repo.load_snapshot(pool, 7)

    assert loaded is not None
    assert loaded.user_id == 7
    assert loaded.events[0].summary == "Лекция"
    fetchrow = next(call for call in connection.calls if call[0] == "fetchrow")
    fetch = next(call for call in connection.calls if call[0] == "fetch")
    assert fetchrow[2] == (7,)
    assert fetch[2] == (7,)
    assert connection.calls[0] == ("transaction", (), {"isolation": "repeatable_read"})


def make_bot() -> tuple[Bot, list[object]]:
    bot = Bot(TOKEN)
    sent: list[object] = []

    async def fake_session(bot_instance: Bot, method: object, *args: object, **kwargs: object):
        sent.append(method)
        return True

    bot.session = AsyncMock(side_effect=fake_session)
    return bot, sent


def document_message(
    bot: Bot,
    *,
    filename: str = "schedule.ics",
    file_size: int | None = 100,
    sender_id: int | None = 42,
    chat_id: int = 42,
) -> Message:
    user = User(id=sender_id, is_bot=False, first_name="Студент") if sender_id is not None else None
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=chat_id, type="private"),
        from_user=user,
        document=Document(
            file_id="file",
            file_unique_id="unique",
            file_name=filename,
            file_size=file_size,
        ),
    )
    message._bot = bot
    return message


async def test_ics_filter_matches_only_ics_documents() -> None:
    bot, _ = make_bot()
    calendar_filter = ICalendarDocumentFilter()

    assert await calendar_filter(document_message(bot)) is True
    assert await calendar_filter(document_message(bot, filename="schedule.txt")) is False
    assert await calendar_filter(document_message(bot, filename="schedule.ICS")) is True


async def test_handler_imports_document_from_trusted_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, sent = make_bot()
    message = document_message(bot)
    service = MagicMock()
    service.import_calendar = AsyncMock(
        return_value=CalendarImportSummary(
            event_count=1,
            recurring_count=0,
            all_day_count=0,
            timezone=MOSCOW,
            first_date=datetime(2026, 2, 2).date(),
            last_date=datetime(2026, 2, 2).date(),
            imported_at=NOW,
            replaced=False,
        )
    )

    async def fake_download(
        bot_instance: Bot,
        file: object,
        destination: io.BytesIO | None = None,
        **kwargs: object,
    ) -> object:
        assert destination is not None
        destination.write(SMALL_CALENDAR)
        destination.seek(0)
        return destination

    monkeypatch.setattr(Bot, "download", fake_download)

    await handle_calendar_document(message, db=MagicMock(), calendar_import_service=service)

    service.import_calendar.assert_awaited_once_with(42, "schedule.ics", SMALL_CALENDAR)
    messages = [method for method in sent if isinstance(method, SendMessage)]
    assert len(messages) == 1
    assert "Личное расписание импортировано" in messages[0].text
    assert "Событий: 1" in messages[0].text


async def test_handler_rejects_missing_sender_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, sent = make_bot()
    message = document_message(bot, sender_id=None)
    download = AsyncMock()
    monkeypatch.setattr(Bot, "download", download)
    service = MagicMock()
    service.import_calendar = AsyncMock()

    await handle_calendar_document(message, db=MagicMock(), calendar_import_service=service)

    download.assert_not_awaited()
    service.import_calendar.assert_not_awaited()
    messages = [method for method in sent if isinstance(method, SendMessage)]
    assert "только владельцу в личном чате" in messages[0].text


async def test_handler_rejects_foreign_chat_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, _ = make_bot()
    message = document_message(bot, sender_id=42, chat_id=99)
    download = AsyncMock()
    monkeypatch.setattr(Bot, "download", download)
    service = MagicMock()
    service.import_calendar = AsyncMock()

    await handle_calendar_document(message, db=MagicMock(), calendar_import_service=service)

    download.assert_not_awaited()
    service.import_calendar.assert_not_awaited()


async def test_handler_rejects_oversized_document_without_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, _ = make_bot()
    message = document_message(bot, file_size=10**9)
    download = AsyncMock()
    monkeypatch.setattr(Bot, "download", download)
    service = MagicMock()
    service.import_calendar = AsyncMock()

    await handle_calendar_document(message, db=MagicMock(), calendar_import_service=service)

    download.assert_not_awaited()
    service.import_calendar.assert_not_awaited()


def test_bounded_buffer_stops_oversized_download() -> None:
    buffer = BoundedBytesIO(4)

    with pytest.raises(CalendarFileTooLarge):
        buffer.write(b"12345")
