from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulebase, rrulestr
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.schemas.tools import Lesson, ScheduleResult, ToolFailure, WeekSchedule

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MAX_CALENDAR_BYTES = 256 * 1024
MAX_CALENDAR_EVENTS = 5000
MAX_OCCURRENCES_PER_QUERY = 10000
MAX_RECURRENCE_STEPS = 200_000
MAX_SUMMARY_LENGTH = 1000
MAX_UID_LENGTH = 1024
SCHEDULE_UNAVAILABLE_MESSAGE = "Не удалось прочитать расписание. Попробуйте позже."


class CalendarImportErrorCode(StrEnum):
    FILE_TOO_LARGE = "file_too_large"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_CALENDAR = "invalid_calendar"
    UNSUPPORTED_CALENDAR = "unsupported_calendar"
    TOO_MANY_EVENTS = "too_many_events"
    STORAGE_UNAVAILABLE = "storage_unavailable"


@dataclass
class RecurrenceBudget:
    remaining: int = MAX_RECURRENCE_STEPS

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ToolFailure(
                "schedule_unavailable",
                "В расписании слишком много повторений для выбранного интервала.",
            )


class CalendarImportError(ValueError):
    def __init__(self, code: CalendarImportErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def validate_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("timezone must be a valid IANA timezone") from None

    return value


class CalendarImportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_count: int = Field(ge=0)
    recurring_count: int = Field(ge=0)
    all_day_count: int = Field(ge=0)
    timezone: str
    first_date: date | None = None
    last_date: date | None = None
    imported_at: datetime
    replaced: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return validate_zone(value)

    @field_validator("imported_at")
    @classmethod
    def require_aware_imported_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("imported_at must be timezone-aware")
        return value


class StoredCalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: str = Field(min_length=1, max_length=MAX_UID_LENGTH)
    recurrence_id: str = ""
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_LENGTH)
    location: str = Field(default="", max_length=MAX_SUMMARY_LENGTH)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    cancelled: bool = False
    timezone: str
    rrule: str | None = Field(default=None, max_length=2048)
    rdate: tuple[datetime, ...] = ()
    exdate: tuple[datetime, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return validate_zone(value)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calendar event datetimes must be timezone-aware")
        return value

    @field_validator("rdate", "exdate")
    @classmethod
    def require_aware_recurrence_dates(
        cls,
        values: tuple[datetime, ...],
    ) -> tuple[datetime, ...]:
        for value in values:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("recurrence dates must be timezone-aware")
        return values

    @model_validator(mode="after")
    def validate_period(self) -> StoredCalendarEvent:
        if self.ends_at <= self.starts_at:
            raise ValueError("calendar event end must be after its start")
        return self

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at


class CalendarSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: int
    timezone: str
    source_name: str = Field(min_length=1, max_length=255)
    imported_at: datetime
    events: tuple[StoredCalendarEvent, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return validate_zone(value)

    @field_validator("imported_at")
    @classmethod
    def require_aware_imported_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("imported_at must be timezone-aware")
        return value

    def day(self, day: date, timezone: str | None = None) -> ScheduleResult:
        return self._day_result(day, self._display_zone(timezone))

    def week(self, anchor: date, timezone: str | None = None) -> WeekSchedule:
        display_zone = self._display_zone(timezone)
        monday = anchor - timedelta(days=anchor.weekday())
        days = [
            self._day_result(monday + timedelta(days=offset), display_zone) for offset in range(7)
        ]

        return WeekSchedule(
            timezone=display_zone.key,
            start_date=monday.isoformat(),
            end_date=(monday + timedelta(days=6)).isoformat(),
            days=days,
            free_days=[item.date for item in days if item.is_free_day is True],
        )

    def _display_zone(self, timezone: str | None) -> ZoneInfo:
        value = timezone or self.timezone

        try:
            return ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ToolFailure("schedule_unavailable", SCHEDULE_UNAVAILABLE_MESSAGE) from None

    def _day_result(self, day: date, display_zone: ZoneInfo) -> ScheduleResult:
        window_start = datetime.combine(day, time.min, tzinfo=display_zone)
        window_end = window_start + timedelta(days=1)
        occurrences = self._occurrences(window_start, window_end)
        lessons = [
            self._lesson(event, start, end, window_start, window_end, display_zone)
            for start, end, event in occurrences
        ]
        lessons.sort(key=lambda item: (item.start, item.end, item.title))

        return ScheduleResult(
            date=day.isoformat(),
            lessons=lessons,
            data_available=True,
            is_free_day=not lessons,
            weekday=WEEKDAYS[day.weekday()],
        )

    @staticmethod
    def _lesson(
        event: StoredCalendarEvent,
        start: datetime,
        end: datetime,
        window_start: datetime,
        window_end: datetime,
        display_zone: ZoneInfo,
    ) -> Lesson:
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)

        if event.all_day:
            start_text = "00:00"
            end_text = "23:59"
        else:
            local_start = clipped_start.astimezone(display_zone)
            local_end = clipped_end.astimezone(display_zone)
            start_text = local_start.strftime("%H:%M")
            if clipped_end >= window_end:
                end_text = "23:59"
            else:
                end_text = local_end.strftime("%H:%M")

        return Lesson(
            start=start_text,
            end=end_text,
            title=event.summary,
            location=event.location,
        )

    def _occurrences(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[datetime, datetime, StoredCalendarEvent]]:
        masters: list[StoredCalendarEvent] = []
        overrides: dict[str, dict[str, StoredCalendarEvent]] = {}

        for event in self.events:
            if event.recurrence_id:
                overrides.setdefault(event.uid, {})[event.recurrence_id] = event
            else:
                masters.append(event)

        occurrences: list[tuple[datetime, datetime, StoredCalendarEvent]] = []
        budget = RecurrenceBudget()

        try:
            for event in masters:
                event_overrides = overrides.get(event.uid, {})
                event_zone = ZoneInfo(event.timezone)
                duration = event.duration

                for start in self._event_starts(event, window_start, window_end, budget):
                    key = recurrence_key(event, start, event_zone)
                    if key in event_overrides:
                        continue

                    end = start + duration
                    if start < window_end and end > window_start:
                        occurrences.append((start, end, event))

            for event_overrides in overrides.values():
                for event in event_overrides.values():
                    if event.cancelled:
                        continue
                    if event.starts_at < window_end and event.ends_at > window_start:
                        occurrences.append((event.starts_at, event.ends_at, event))
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("schedule_unavailable", SCHEDULE_UNAVAILABLE_MESSAGE) from None

        occurrences.sort(key=lambda item: (item[0], item[1], item[2].summary))
        return occurrences

    @staticmethod
    def _event_starts(
        event: StoredCalendarEvent,
        window_start: datetime,
        window_end: datetime,
        budget: RecurrenceBudget,
    ) -> list[datetime]:
        starts: set[datetime] = {event.starts_at}
        event_zone = ZoneInfo(event.timezone)

        if event.rrule:
            rule = build_occurrence_rule(event)
            search_start = window_start - event.duration

            if event.all_day:
                local_search_start = search_start.astimezone(event_zone).replace(tzinfo=None)
                local_window_end = window_end.astimezone(event_zone).replace(tzinfo=None)
                generated = bounded_starts(
                    rule,
                    local_search_start,
                    local_window_end,
                    budget,
                )
                starts.update(
                    datetime.combine(value.date(), time.min, tzinfo=event_zone)
                    for value in generated
                )
            else:
                starts.update(bounded_starts(rule, search_start, window_end, budget))

        starts.update(event.rdate)
        excluded = {recurrence_key(event, value, event_zone) for value in event.exdate}

        return sorted(
            value for value in starts if recurrence_key(event, value, event_zone) not in excluded
        )


def empty_day_schedule(day: date, timezone: str) -> ScheduleResult:
    validate_zone(timezone)

    return ScheduleResult(
        date=day.isoformat(),
        lessons=[],
        data_available=False,
        is_free_day=None,
        weekday=WEEKDAYS[day.weekday()],
    )


def empty_week_schedule(anchor: date, timezone: str) -> WeekSchedule:
    timezone = validate_zone(timezone)
    monday = anchor - timedelta(days=anchor.weekday())
    days = [empty_day_schedule(monday + timedelta(days=offset), timezone) for offset in range(7)]

    return WeekSchedule(
        timezone=timezone,
        start_date=monday.isoformat(),
        end_date=(monday + timedelta(days=6)).isoformat(),
        days=days,
        free_days=[],
    )


def recurrence_key(
    event: StoredCalendarEvent,
    occurrence: datetime,
    event_zone: ZoneInfo,
) -> str:
    if occurrence.tzinfo is None or occurrence.utcoffset() is None:
        occurrence = occurrence.replace(tzinfo=event_zone)

    if event.all_day:
        return occurrence.astimezone(event_zone).date().isoformat()

    return occurrence.astimezone(UTC).isoformat()


def build_occurrence_rule(event: StoredCalendarEvent) -> rrulebase:
    if not event.rrule:
        raise ValueError("calendar event has no recurrence rule")

    event_zone = ZoneInfo(event.timezone)
    local_start = event.starts_at.astimezone(event_zone)

    if event.all_day:
        naive_start = local_start.replace(tzinfo=None)
        return rrulestr(event.rrule, dtstart=naive_start, ignoretz=True)

    return rrulestr(
        normalize_until(event.rrule, local_start),
        dtstart=local_start,
    )


def normalize_until(rule_text: str, dtstart: datetime) -> str:
    parts: list[str] = []

    for part in rule_text.split(";"):
        name, separator, value = part.partition("=")
        if not separator or name.upper() != "UNTIL" or value.upper().endswith("Z"):
            parts.append(part)
            continue

        parsed = parse_until(value)
        local_until = parsed.replace(tzinfo=dtstart.tzinfo)
        utc_until = local_until.astimezone(UTC)
        parts.append(f"UNTIL={utc_until.strftime('%Y%m%dT%H%M%SZ')}")

    return ";".join(parts)


def parse_until(value: str) -> datetime:
    formats = ("%Y%m%dT%H%M%S", "%Y%m%d")

    for value_format in formats:
        try:
            return datetime.strptime(value, value_format)
        except ValueError:
            continue

    raise ValueError("invalid UNTIL value")


def bounded_starts(
    rule: rrulebase,
    window_start: datetime,
    window_end: datetime,
    budget: RecurrenceBudget,
) -> list[datetime]:
    result: list[datetime] = []

    for occurrence in rule:
        budget.consume()
        if occurrence >= window_end:
            break
        if occurrence < window_start:
            continue

        result.append(occurrence)
        if len(result) > MAX_OCCURRENCES_PER_QUERY:
            raise ToolFailure(
                "schedule_unavailable",
                "В расписании слишком много повторений для выбранного интервала.",
            )

    return result
