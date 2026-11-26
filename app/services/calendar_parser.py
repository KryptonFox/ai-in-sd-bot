from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar
from icalendar.cal import Component

from app.core.schemas.calendar import (
    MAX_CALENDAR_BYTES,
    MAX_CALENDAR_EVENTS,
    MAX_SUMMARY_LENGTH,
    MAX_UID_LENGTH,
    CalendarImportError,
    CalendarImportErrorCode,
    StoredCalendarEvent,
    build_occurrence_rule,
    validate_zone,
)
from app.core.timezones import DEFAULT_USER_TIMEZONE

ALLOWED_REPEAT_FREQUENCIES = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
UNSUPPORTED_REPEAT_PARTS = frozenset({"BYSECOND"})


def parse_calendar(
    data: bytes,
    timezone: str,
    *,
    max_bytes: int = MAX_CALENDAR_BYTES,
    max_events: int = MAX_CALENDAR_EVENTS,
) -> list[StoredCalendarEvent]:
    """Parse VEVENT blocks and reject constructs that cannot be expanded safely."""
    if not data:
        raise CalendarImportError(
            CalendarImportErrorCode.INVALID_CALENDAR,
            "Файл календаря пуст.",
        )
    if len(data) > max_bytes:
        raise CalendarImportError(
            CalendarImportErrorCode.FILE_TOO_LARGE,
            f"Файл календаря больше {max_bytes // 1024} КБ.",
        )

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CalendarImportError(
            CalendarImportErrorCode.INVALID_ENCODING,
            "Файл календаря должен быть в кодировке UTF-8.",
        ) from None

    if "\x00" in text:
        raise CalendarImportError(
            CalendarImportErrorCode.INVALID_CALENDAR,
            "Файл календаря повреждён.",
        )

    try:
        parsed = Calendar.from_ical(text)
    except Exception:
        raise CalendarImportError(
            CalendarImportErrorCode.INVALID_CALENDAR,
            "Не удалось разобрать файл iCalendar.",
        ) from None

    if isinstance(parsed, list) or not isinstance(parsed, Calendar):
        raise CalendarImportError(
            CalendarImportErrorCode.INVALID_CALENDAR,
            "Файл должен содержать один календарь iCalendar.",
        )

    try:
        user_zone = ZoneInfo(validate_zone(timezone))
    except ValueError:
        user_zone = ZoneInfo(DEFAULT_USER_TIMEZONE)

    events: list[StoredCalendarEvent] = []

    for component in parsed.walk("VEVENT"):
        try:
            event = _parse_event(component, user_zone)
        except CalendarImportError:
            raise
        except Exception:
            raise CalendarImportError(
                CalendarImportErrorCode.INVALID_CALENDAR,
                "В календаре обнаружены некорректные данные события.",
            ) from None
        if event is not None:
            events.append(event)

    if len(events) > max_events:
        raise CalendarImportError(
            CalendarImportErrorCode.TOO_MANY_EVENTS,
            f"В календаре больше {max_events} событий.",
        )

    _validate_unique_events(events)
    return events


def _parse_event(component: Component, user_zone: ZoneInfo) -> StoredCalendarEvent | None:
    status = _text_value(component, "STATUS").strip().casefold()
    cancelled = status == "cancelled"
    if cancelled and _first(component.get("RECURRENCE-ID")) is None:
        return None

    summary = _clean_text(_text_value(component, "SUMMARY")) or "(без названия)"
    location = _clean_text(_text_value(component, "LOCATION"))
    starts_at, ends_at, all_day, event_timezone = _parse_period(component, user_zone)
    uid = _event_uid(component, summary, starts_at, location)
    recurrence_id = _recurrence_id(component, user_zone, all_day)

    if component.get("EXRULE") is not None:
        raise _unsupported("Исключающие правила повторения EXRULE не поддерживаются.")

    rrule = _recurrence_rule(component)
    event = StoredCalendarEvent(
        uid=uid,
        recurrence_id=recurrence_id,
        summary=summary,
        location=location,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        cancelled=cancelled,
        timezone=event_timezone,
        rrule=rrule,
    )

    if rrule is not None:
        try:
            build_occurrence_rule(event)
        except CalendarImportError:
            raise
        except Exception:
            raise _unsupported("Правило повторения события не поддерживается.") from None

    return event.model_copy(
        update={
            "rdate": _recurrence_dates(component, "RDATE", user_zone, all_day, event_timezone),
            "exdate": _recurrence_dates(component, "EXDATE", user_zone, all_day, event_timezone),
        }
    )


def _parse_period(
    component: Component,
    user_zone: ZoneInfo,
) -> tuple[datetime, datetime, bool, str]:
    dtstart_property = _first(component.get("DTSTART"))
    if dtstart_property is None:
        raise _unsupported("У события отсутствует дата начала.")

    dtstart = getattr(dtstart_property, "dt", None)
    if isinstance(dtstart, datetime):
        all_day = False
        starts_at, event_timezone = _aware_datetime(dtstart, dtstart_property, user_zone)
    elif isinstance(dtstart, date):
        all_day = True
        event_timezone = user_zone.key
        starts_at = datetime.combine(dtstart, time.min, tzinfo=user_zone)
    else:
        raise _unsupported("Событие содержит неподдерживаемую дату начала.")

    ends_at = _event_end(component, starts_at, user_zone, event_timezone, all_day)
    if ends_at <= starts_at:
        raise _unsupported("Событие заканчивается раньше, чем начинается.")

    return starts_at, ends_at, all_day, event_timezone


def _event_end(
    component: Component,
    starts_at: datetime,
    user_zone: ZoneInfo,
    event_timezone: str,
    all_day: bool,
) -> datetime:
    dtend_property = _first(component.get("DTEND"))

    if dtend_property is not None:
        dtend = getattr(dtend_property, "dt", None)
        if isinstance(dtend, datetime):
            if all_day:
                raise _unsupported("Событие на весь день содержит время окончания.")
            ends_at, _ = _aware_datetime(dtend, dtend_property, user_zone)
            return ends_at
        if isinstance(dtend, date):
            if not all_day:
                raise _unsupported("Событие со временем содержит дату окончания.")
            return datetime.combine(dtend, time.min, tzinfo=ZoneInfo(event_timezone))

        raise _unsupported("Событие содержит неподдерживаемую дату окончания.")

    duration_property = _first(component.get("DURATION"))
    if duration_property is not None:
        duration = getattr(duration_property, "dt", None)
        if not isinstance(duration, timedelta) or duration <= timedelta(0):
            raise _unsupported("Событие содержит неподдерживаемую длительность.")
        return starts_at + duration

    if all_day:
        return starts_at + timedelta(days=1)

    return starts_at + timedelta(hours=1)


def _aware_datetime(
    value: datetime,
    property_value: Any,
    user_zone: ZoneInfo,
) -> tuple[datetime, str]:
    if value.tzinfo is not None and value.utcoffset() is not None:
        timezone = _property_timezone(property_value, value, user_zone)
        return value, timezone

    tzid = _property_parameter(property_value, "TZID")
    if tzid is not None:
        zone = _resolve_zone(tzid)
        if zone is None:
            raise _unsupported(f"Неизвестный часовой пояс события: {tzid}.")
        return value.replace(tzinfo=zone), zone.key

    return value.replace(tzinfo=user_zone), user_zone.key


def _property_timezone(property_value: Any, value: datetime, user_zone: ZoneInfo) -> str:
    tzid = _property_parameter(property_value, "TZID")
    if tzid is not None:
        zone = _resolve_zone(tzid)
        if zone is not None:
            return zone.key

    key = getattr(value.tzinfo, "key", None)
    if isinstance(key, str):
        try:
            return validate_zone(key)
        except ValueError:
            pass

    if value.utcoffset() == timedelta(0):
        return "UTC"

    return user_zone.key


def _resolve_zone(value: str) -> ZoneInfo | None:
    candidate = value.strip().strip('"').strip("/")
    if not candidate:
        return None

    parts = candidate.split("/")
    for index in range(len(parts)):
        suffix = "/".join(parts[index:])
        try:
            return ZoneInfo(validate_zone(suffix))
        except (ValueError, ZoneInfoNotFoundError):
            continue

    return None


def _event_uid(
    component: Component,
    summary: str,
    starts_at: datetime,
    location: str,
) -> str:
    uid = _clean_text(_text_value(component, "UID"))

    if not uid:
        basis = f"{summary}|{starts_at.isoformat()}|{location}"
        uid = f"generated-{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]}"

    if len(uid) > MAX_UID_LENGTH:
        uid = f"sha256-{hashlib.sha256(uid.encode('utf-8')).hexdigest()}"

    return uid


def _recurrence_id(
    component: Component,
    user_zone: ZoneInfo,
    all_day: bool,
) -> str:
    property_value = _first(component.get("RECURRENCE-ID"))
    if property_value is None:
        return ""

    if _property_parameter(property_value, "RANGE") is not None:
        raise _unsupported("Диапазоны RECURRENCE-ID не поддерживаются.")

    value = getattr(property_value, "dt", None)
    if isinstance(value, datetime):
        if all_day:
            raise _unsupported("Событие на весь день содержит время исключения повторения.")
        aware, _ = _aware_datetime(value, property_value, user_zone)
        return aware.astimezone(UTC).isoformat()

    if isinstance(value, date):
        if not all_day:
            raise _unsupported("Событие со временем содержит дату исключения повторения.")
        return value.isoformat()

    raise _unsupported("Событие содержит неподдерживаемый идентификатор повторения.")


def _recurrence_rule(component: Component) -> str | None:
    property_value = _first(component.get("RRULE"))
    if property_value is None:
        return None

    try:
        rule = property_value.to_ical().decode("utf-8").strip()
    except Exception:
        raise _unsupported("Правило повторения события не читается.") from None

    if not rule:
        raise _unsupported("Правило повторения события пустое.")
    if len(rule) > 2048:
        raise _unsupported("Правило повторения события слишком длинное.")

    upper_rule = rule.upper()
    if any(token in upper_rule for token in UNSUPPORTED_REPEAT_PARTS):
        raise _unsupported("Секундные повторения в расписании не поддерживаются.")

    frequency = _rule_frequency(upper_rule)
    if frequency not in ALLOWED_REPEAT_FREQUENCIES:
        raise _unsupported(
            "Поддерживаются только ежедневные, еженедельные, ежемесячные и ежегодные повторения."
        )

    return rule


def _rule_frequency(rule: str) -> str:
    for part in rule.split(";"):
        name, separator, value = part.partition("=")
        if separator and name == "FREQ":
            return value

    return ""


def _recurrence_dates(
    component: Component,
    name: str,
    user_zone: ZoneInfo,
    all_day: bool,
    event_timezone: str,
) -> tuple[datetime, ...]:
    result: list[datetime] = []

    for value, property_value in _property_values(component, name):
        if isinstance(value, tuple):
            raise _unsupported("Периоды RDATE и EXDATE не поддерживаются.")

        if isinstance(value, datetime):
            if all_day:
                aware, _ = _aware_datetime(value, property_value, user_zone)
                event_zone = ZoneInfo(event_timezone)
                local = aware.astimezone(event_zone)
                result.append(datetime.combine(local.date(), time.min, tzinfo=event_zone))
            else:
                aware, _ = _aware_datetime(value, property_value, user_zone)
                result.append(aware)
            continue

        if isinstance(value, date):
            result.append(datetime.combine(value, time.min, tzinfo=ZoneInfo(event_timezone)))
            continue

        raise _unsupported("Событие содержит неподдерживаемую дату повторения.")

    return tuple(result)


def _property_values(component: Component, name: str) -> list[tuple[object, Any]]:
    value = component.get(name)
    if value is None:
        return []

    groups = value if isinstance(value, list) else [value]
    result: list[tuple[object, Any]] = []

    for group in groups:
        entries = getattr(group, "dts", None)
        if entries is None:
            result.append((getattr(group, "dt", group), group))
            continue

        for entry in entries:
            result.append((getattr(entry, "dt", entry), entry))

    return result


def _property_parameter(property_value: Any, name: str) -> str | None:
    parameters = getattr(property_value, "params", None)
    if parameters is None:
        return None

    value = parameters.get(name)
    if isinstance(value, list):
        value = value[0] if value else None

    return str(value) if value is not None else None


def _first(value: object) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text_value(component: Component, name: str) -> str:
    value = _first(component.get(name))
    if value is None:
        return ""

    return str(value)


def _clean_text(value: str, *, limit: int = MAX_SUMMARY_LENGTH) -> str:
    normalized = " ".join(value.split())
    normalized = "".join(character for character in normalized if ord(character) >= 32)

    return normalized[:limit]


def _unsupported(message: str) -> CalendarImportError:
    return CalendarImportError(CalendarImportErrorCode.UNSUPPORTED_CALENDAR, message)


def _validate_unique_events(events: list[StoredCalendarEvent]) -> None:
    masters = {event.uid for event in events if not event.recurrence_id}
    keys: set[tuple[str, str]] = set()

    for event in events:
        key = (event.uid, event.recurrence_id)
        if key in keys:
            raise _unsupported("В календаре есть повторяющиеся события с одинаковым UID.")
        keys.add(key)

        if event.recurrence_id and event.uid not in masters:
            raise _unsupported("Исключение повторения не имеет основного события.")
