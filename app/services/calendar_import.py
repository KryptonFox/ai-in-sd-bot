from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from app.core.clock import Clock, SystemClock
from app.core.schemas.calendar import (
    MAX_CALENDAR_BYTES,
    CalendarImportError,
    CalendarImportErrorCode,
    CalendarImportSummary,
    CalendarSnapshot,
    StoredCalendarEvent,
    validate_zone,
)
from app.core.timezones import DEFAULT_USER_TIMEZONE
from app.db.repositories.calendar_repo import replace_calendar
from app.db.repositories.timezone_repo import get_timezone
from app.services.calendar_parser import parse_calendar

STORAGE_UNAVAILABLE_MESSAGE = "Не удалось сохранить расписание. Прежнее расписание сохранено."


def is_ics_filename(filename: str | None) -> bool:
    if not filename:
        return False

    return filename.casefold().endswith(".ics")


def _summary_dates(
    events: list[StoredCalendarEvent],
    timezone: str,
) -> tuple[date | None, date | None]:
    if not events:
        return None, None

    zone = ZoneInfo(timezone)
    dates = [event.starts_at.astimezone(zone).date() for event in events]

    return min(dates), max(dates)


class CalendarImportService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        clock: Clock | None = None,
        *,
        max_bytes: int = MAX_CALENDAR_BYTES,
    ) -> None:
        self._pool = pool
        self._clock = clock or SystemClock()
        self._max_bytes = max_bytes

    async def import_calendar(
        self,
        user_id: int,
        filename: str | None,
        data: bytes,
    ) -> CalendarImportSummary:
        if not is_ics_filename(filename):
            raise CalendarImportError(
                CalendarImportErrorCode.INVALID_CALENDAR,
                "Пришлите файл календаря с расширением .ics.",
            )

        try:
            user_timezone = await get_timezone(self._pool, user_id) or DEFAULT_USER_TIMEZONE
            user_timezone = validate_zone(user_timezone)
        except ValueError:
            user_timezone = DEFAULT_USER_TIMEZONE
        except Exception:
            raise CalendarImportError(
                CalendarImportErrorCode.STORAGE_UNAVAILABLE,
                STORAGE_UNAVAILABLE_MESSAGE,
            ) from None

        events = parse_calendar(data, user_timezone, max_bytes=self._max_bytes)
        imported_at = self._clock.now()
        source_name = Path(filename or "schedule.ics").name[:255] or "schedule.ics"
        snapshot = CalendarSnapshot(
            user_id=user_id,
            timezone=user_timezone,
            source_name=source_name,
            imported_at=imported_at,
            events=tuple(events),
        )

        try:
            replaced = await replace_calendar(self._pool, snapshot)
        except Exception:
            raise CalendarImportError(
                CalendarImportErrorCode.STORAGE_UNAVAILABLE,
                STORAGE_UNAVAILABLE_MESSAGE,
            ) from None

        first_date, last_date = _summary_dates(events, user_timezone)

        return CalendarImportSummary(
            event_count=len(events),
            recurring_count=sum(1 for event in events if event.rrule is not None),
            all_day_count=sum(1 for event in events if event.all_day),
            timezone=user_timezone,
            first_date=first_date,
            last_date=last_date,
            imported_at=imported_at,
            replaced=replaced,
        )
