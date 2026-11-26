from __future__ import annotations

import json

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from app.db.notification_records import (
    CityRecord,
    CitySelectionRecord,
    CitySelectionState,
    NotificationKind,
    NotificationSettingsRecord,
    NotificationState,
    OutgoingNotificationRecord,
)


def decode_city(row: asyncpg.Record) -> CityRecord | None:
    name = row["city_name"]
    country = row["city_country"]
    latitude = row["city_latitude"]
    longitude = row["city_longitude"]
    if name is None or country is None or latitude is None or longitude is None:
        return None

    return CityRecord(
        name=name,
        country=country,
        region=row["city_region"],
        latitude=latitude,
        longitude=longitude,
    )


def decode_settings(row: asyncpg.Record) -> NotificationSettingsRecord:
    return NotificationSettingsRecord(
        user_id=row["user_id"],
        destination_chat_id=row["destination_chat_id"],
        city=decode_city(row),
        city_confirmed_at=row["city_confirmed_at"],
        briefing_enabled=row["briefing_enabled"],
        briefing_consent_at=row["briefing_consent_at"],
        timezone=row["timezone"],
        updated_at=row["updated_at"],
    )


def decode_city_selection(row: asyncpg.Record) -> CitySelectionRecord:
    raw_candidates = row["candidates"]
    if isinstance(raw_candidates, str):
        raw_candidates = json.loads(raw_candidates)

    candidates = tuple(CityRecord(**candidate) for candidate in raw_candidates)
    return CitySelectionRecord(
        id=row["id"],
        user_id=row["user_id"],
        candidates=candidates,
        state=CitySelectionState(row["state"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        selected_index=row["selected_index"],
    )


def decode_outgoing(row: asyncpg.Record) -> OutgoingNotificationRecord:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    return OutgoingNotificationRecord(
        id=row["id"],
        event_key=row["event_key"],
        user_id=row["user_id"],
        kind=NotificationKind(row["kind"]),
        destination_chat_id=row["destination_chat_id"],
        payload=payload,
        state=NotificationState(row["state"]),
        attempt_count=row["attempt_count"],
        scheduled_for=row["scheduled_for"],
        next_attempt_at=row["next_attempt_at"],
        lease_id=row["lease_id"],
        lease_until=row["lease_until"],
        trace_id=row["trace_id"],
    )


SETTINGS_QUERY = """
SELECT
    s.user_id,
    s.destination_chat_id,
    s.city_name,
    s.city_country,
    s.city_region,
    s.city_latitude,
    s.city_longitude,
    s.city_confirmed_at,
    s.briefing_enabled,
    s.briefing_consent_at,
    s.updated_at,
    u.timezone
FROM notification_settings s
LEFT JOIN user_settings u ON u.user_id = s.user_id
"""


async def fetch_settings(
    conn: asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record],
    user_id: int,
) -> NotificationSettingsRecord | None:
    row = await conn.fetchrow(f"{SETTINGS_QUERY} WHERE s.user_id = $1", user_id)
    return decode_settings(row) if row is not None else None


class PoolRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool


class SettingsReaderRepository(PoolRepository):
    async def settings(self, user_id: int) -> NotificationSettingsRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"{SETTINGS_QUERY} WHERE s.user_id = $1", user_id)
        return decode_settings(row) if row is not None else None
