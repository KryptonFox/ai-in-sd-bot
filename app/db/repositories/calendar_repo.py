from datetime import datetime

import asyncpg

from app.core.schemas.calendar import (
    CalendarSnapshot,
    StoredCalendarEvent,
)

UPSERT_CALENDAR_SQL = """
INSERT INTO personal_calendars (
    user_id, timezone, source_name, imported_at, event_count, updated_at
)
VALUES ($1, $2, $3, $4, $5, NOW())
ON CONFLICT (user_id) DO UPDATE
SET timezone = EXCLUDED.timezone,
    source_name = EXCLUDED.source_name,
    imported_at = EXCLUDED.imported_at,
    event_count = EXCLUDED.event_count,
    updated_at = NOW()
"""

INSERT_EVENT_SQL = """
INSERT INTO personal_calendar_events (
    user_id, uid, recurrence_id, summary, location,
    starts_at, ends_at, all_day, cancelled, timezone, rrule, rdate, exdate
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""


async def replace_calendar(pool: asyncpg.Pool, snapshot: CalendarSnapshot) -> bool:
    """Replace a snapshot atomically; the calendar upsert row lock serializes re-imports."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO user_settings (user_id) VALUES ($1)
               ON CONFLICT (user_id) DO NOTHING""",
            snapshot.user_id,
        )
        existed = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM personal_calendars WHERE user_id = $1)",
            snapshot.user_id,
        )
        await conn.execute(
            UPSERT_CALENDAR_SQL,
            snapshot.user_id,
            snapshot.timezone,
            snapshot.source_name,
            snapshot.imported_at,
            len(snapshot.events),
        )
        await conn.execute(
            "DELETE FROM personal_calendar_events WHERE user_id = $1",
            snapshot.user_id,
        )

        if snapshot.events:
            await conn.executemany(
                INSERT_EVENT_SQL,
                [_event_args(snapshot.user_id, event) for event in snapshot.events],
            )

    return bool(existed)


async def load_snapshot(pool: asyncpg.Pool, user_id: int) -> CalendarSnapshot | None:
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read"):
            calendar = await conn.fetchrow(
                """SELECT user_id, timezone, source_name, imported_at
                   FROM personal_calendars WHERE user_id = $1""",
                user_id,
            )
            if calendar is None:
                return None

            rows = await conn.fetch(
                """SELECT uid, recurrence_id, summary, location,
                          starts_at, ends_at, all_day, cancelled, timezone,
                          rrule, rdate, exdate
                   FROM personal_calendar_events
                   WHERE user_id = $1
                   ORDER BY starts_at, id""",
                user_id,
            )

    return CalendarSnapshot(
        user_id=calendar["user_id"],
        timezone=calendar["timezone"],
        source_name=calendar["source_name"],
        imported_at=calendar["imported_at"],
        events=tuple(_event_from_row(row) for row in rows),
    )


def _event_args(user_id: int, event: StoredCalendarEvent) -> tuple[object, ...]:
    return (
        user_id,
        event.uid,
        event.recurrence_id,
        event.summary,
        event.location,
        event.starts_at,
        event.ends_at,
        event.all_day,
        event.cancelled,
        event.timezone,
        event.rrule,
        list(event.rdate),
        list(event.exdate),
    )


def _event_from_row(row: asyncpg.Record) -> StoredCalendarEvent:
    return StoredCalendarEvent(
        uid=row["uid"],
        recurrence_id=row["recurrence_id"],
        summary=row["summary"],
        location=row["location"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        all_day=row["all_day"],
        cancelled=row["cancelled"],
        timezone=row["timezone"],
        rrule=row["rrule"],
        rdate=_datetime_tuple(row["rdate"]),
        exdate=_datetime_tuple(row["exdate"]),
    )


def _datetime_tuple(values: object) -> tuple[datetime, ...]:
    if values is None:
        return ()

    if isinstance(values, (list, tuple)):
        return tuple(values)

    raise ValueError("calendar recurrence dates must be a list of datetimes")
