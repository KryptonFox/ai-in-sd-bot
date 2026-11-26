from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid_zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None

    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def briefing_window_open(local_now: datetime, start: time, end: time) -> bool:
    local_time = local_now.time()
    return start <= local_time <= end


def first_valid_local_moment(naive_local: datetime, zone: ZoneInfo) -> datetime:
    """Return the earliest UTC instant for a local wall time, handling DST gaps."""
    for offset_minutes in range(0, 24 * 60):
        candidate = naive_local + timedelta(minutes=offset_minutes)
        candidates: list[datetime] = []
        for fold in (0, 1):
            zoned = candidate.replace(tzinfo=zone, fold=fold)
            utc_value = zoned.astimezone(UTC)
            round_trip = utc_value.astimezone(zone).replace(tzinfo=None)
            if round_trip == candidate:
                candidates.append(utc_value)

        if candidates:
            return min(candidates)

    raise ValueError("local time is not representable")


def briefing_scheduled_at(local_date: date, zone: ZoneInfo, start: time) -> datetime:
    naive_local = datetime.combine(local_date, start)
    return first_valid_local_moment(naive_local, zone)
