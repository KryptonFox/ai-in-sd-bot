from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.core.clock import Clock
from app.core.schemas.tools import Lesson, ScheduleResult, ToolFailure, WeekSchedule


class ScheduleService:
    def __init__(self, path: Path, timezone: str, clock: Clock) -> None:
        self._path = path
        self._timezone = timezone
        self._clock = clock
        try:
            self._zone = ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc

    async def get_schedule(self, date: str | None = None) -> ScheduleResult:
        if date is None:
            date = self._clock.now().astimezone(self._zone).date().isoformat()
        parsed = self._parse_date(date)
        data = self._read_data()
        raw_lessons = data.get(date, [])
        try:
            lessons = [Lesson.model_validate(lesson) for lesson in raw_lessons]
        except (ValidationError, TypeError) as exc:
            raise ToolFailure(
                "schedule_unavailable", "В расписании обнаружены некорректные данные."
            ) from exc
        weekdays = (
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        )
        return ScheduleResult(
            date=date,
            lessons=lessons,
            data_available=date in data,
            is_free_day=not lessons if date in data else None,
            weekday=weekdays[parsed.weekday()],
        )

    async def current_week(self) -> WeekSchedule:
        return await self.relative_week(0)

    async def relative_week(self, offset: int) -> WeekSchedule:
        today = self._clock.now().astimezone(self._zone).date()
        return await self.week_containing((today + timedelta(weeks=offset)).isoformat())

    async def week_containing(self, value: str) -> WeekSchedule:
        day = self._parse_date(value)
        monday = day - timedelta(days=day.weekday())
        days: list[ScheduleResult] = []
        for offset in range(7):
            current = monday + timedelta(days=offset)
            days.append(await self.get_schedule(current.isoformat()))
        return WeekSchedule(
            timezone=self._timezone,
            start_date=monday.isoformat(),
            end_date=(monday + timedelta(days=6)).isoformat(),
            days=days,
            free_days=[day.date for day in days if day.is_free_day is True],
        )

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ToolFailure(
                "invalid_date", "Дата должна быть реальной датой в формате YYYY-MM-DD."
            ) from exc
        if parsed.isoformat() != value:
            raise ToolFailure(
                "invalid_date", "Дата должна быть реальной датой в формате YYYY-MM-DD."
            )
        return parsed

    def _read_data(self) -> dict[str, list[dict[str, str]]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolFailure("schedule_unavailable", "Расписание сейчас недоступно.") from exc
        if not isinstance(data, dict):
            raise ToolFailure("schedule_unavailable", "Файл расписания имеет неверный формат.")
        return data
