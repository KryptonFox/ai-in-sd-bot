from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from app.core.clock import Clock, SystemClock
from app.core.schemas.calendar import (
    CalendarSnapshot,
    empty_day_schedule,
    empty_week_schedule,
    validate_zone,
)
from app.core.schemas.tools import ScheduleResult, ToolFailure, WeekSchedule
from app.core.timezones import DEFAULT_USER_TIMEZONE
from app.db.repositories.calendar_repo import load_snapshot
from app.db.repositories.timezone_repo import get_timezone

INVALID_DATE_MESSAGE = "Дата должна быть реальной датой в формате YYYY-MM-DD."
READ_UNAVAILABLE_MESSAGE = "Не удалось прочитать расписание. Попробуйте позже."


class PersonalScheduleService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        clock: Clock | None = None,
        default_timezone: str = DEFAULT_USER_TIMEZONE,
    ) -> None:
        self._pool = pool
        self._clock = clock or SystemClock()
        self._default_timezone = validate_zone(default_timezone)

    async def load_snapshot(self, user_id: int) -> CalendarSnapshot | None:
        try:
            return await load_snapshot(self._pool, user_id)
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("schedule_unavailable", READ_UNAVAILABLE_MESSAGE) from None

    async def get_schedule(
        self,
        user_id: int,
        date_value: str | None = None,
    ) -> ScheduleResult:
        day = self._parse_date(date_value) if date_value is not None else None
        snapshot = await self.load_snapshot(user_id)
        timezone = await self._timezone(user_id, snapshot)

        if day is None:
            day = self._today(timezone)
        if snapshot is None:
            return empty_day_schedule(day, timezone)

        return snapshot.day(day, timezone)

    async def current_week(self, user_id: int) -> WeekSchedule:
        return await self.relative_week(user_id, 0)

    async def relative_week(self, user_id: int, offset: int) -> WeekSchedule:
        snapshot = await self.load_snapshot(user_id)
        timezone = await self._timezone(user_id, snapshot)
        anchor = self._today(timezone) + timedelta(weeks=offset)

        if snapshot is None:
            return empty_week_schedule(anchor, timezone)

        return snapshot.week(anchor, timezone)

    async def week_containing(self, user_id: int, value: str) -> WeekSchedule:
        anchor = self._parse_date(value)
        snapshot = await self.load_snapshot(user_id)
        timezone = await self._timezone(user_id, snapshot)

        if snapshot is None:
            return empty_week_schedule(anchor, timezone)

        return snapshot.week(anchor, timezone)

    async def day_schedule(
        self,
        user_id: int,
        day: date,
        *,
        timezone: str | None = None,
    ) -> ScheduleResult | None:
        snapshot = await self.load_snapshot(user_id)
        if snapshot is None:
            return None

        display_timezone = await self._timezone(user_id, snapshot, timezone)
        return snapshot.day(day, display_timezone)

    async def week_schedule(
        self,
        user_id: int,
        anchor: date,
        *,
        timezone: str | None = None,
    ) -> WeekSchedule | None:
        snapshot = await self.load_snapshot(user_id)
        if snapshot is None:
            return None

        display_timezone = await self._timezone(user_id, snapshot, timezone)
        return snapshot.week(anchor, display_timezone)

    def _today(self, timezone: str) -> date:
        return self._clock.now().astimezone(ZoneInfo(timezone)).date()

    async def _timezone(
        self,
        user_id: int,
        snapshot: CalendarSnapshot | None,
        explicit: str | None = None,
    ) -> str:
        try:
            if explicit is not None:
                return validate_zone(explicit)

            stored = await get_timezone(self._pool, user_id)
            if stored is not None:
                return validate_zone(stored)
        except ValueError:
            pass
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("schedule_unavailable", READ_UNAVAILABLE_MESSAGE) from None

        if snapshot is not None:
            return snapshot.timezone

        return self._default_timezone

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ToolFailure("invalid_date", INVALID_DATE_MESSAGE) from None

        if parsed.isoformat() != value:
            raise ToolFailure("invalid_date", INVALID_DATE_MESSAGE)

        return parsed
