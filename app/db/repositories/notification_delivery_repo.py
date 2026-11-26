from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from uuid import UUID

from app.db.notification_records import (
    NotificationSettingsRecord,
    NotificationState,
    OutgoingNotificationRecord,
)
from app.db.repositories.notification_common import (
    SETTINGS_QUERY,
    SettingsReaderRepository,
    decode_outgoing,
    decode_settings,
)


class PostgresNotificationDeliveryRepository(SettingsReaderRepository):
    async def prepare_reminder_events(
        self,
        now: datetime,
        grace: timedelta,
    ) -> int:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH due AS (
                    SELECT
                        r.id,
                        r.user_id,
                        r.text,
                        r.timezone,
                        r.remind_at,
                        s.destination_chat_id,
                        CASE
                            WHEN r.remind_at < ($1::timestamptz - $2::interval) THEN 'missed'
                            WHEN s.destination_chat_id IS NULL THEN 'suppressed'
                            ELSE 'pending'
                        END AS state,
                        CASE
                            WHEN r.remind_at < (
                                $1::timestamptz - $2::interval
                            ) THEN 'window_expired'
                            WHEN s.destination_chat_id IS NULL THEN 'destination_unavailable'
                            ELSE NULL
                        END AS error_code
                    FROM reminders r
                    LEFT JOIN notification_settings s ON s.user_id = r.user_id
                    WHERE r.remind_at <= $1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM outgoing_notifications o
                          WHERE o.event_key = 'reminder:' || r.id
                      )
                )
                INSERT INTO outgoing_notifications
                    (id, event_key, user_id, kind, destination_chat_id, payload,
                     state, attempt_count, scheduled_for, next_attempt_at,
                     last_error_code, trace_id, created_at, updated_at)
                SELECT
                    gen_random_uuid(),
                    'reminder:' || due.id,
                    due.user_id,
                    'reminder',
                    due.destination_chat_id,
                    jsonb_build_object('text', due.text, 'timezone', due.timezone),
                    due.state,
                    0,
                    due.remind_at,
                    $1,
                    due.error_code,
                    gen_random_uuid(),
                    $1,
                    $1
                FROM due
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id
                """,
                now,
                grace,
            )
        return len(rows)

    async def briefing_candidates(self) -> list[NotificationSettingsRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                {SETTINGS_QUERY}
                WHERE s.briefing_enabled
                  AND s.briefing_consent_at IS NOT NULL
                  AND s.destination_chat_id IS NOT NULL
                  AND s.city_confirmed_at IS NOT NULL
                  AND s.city_name IS NOT NULL
                  AND u.timezone IS NOT NULL
                ORDER BY s.user_id
                """
            )
        return [decode_settings(row) for row in rows]

    async def create_briefing_event(
        self,
        user_id: int,
        local_date: date,
        timezone: str,
        scheduled_for: datetime,
        trace_id: UUID,
        now: datetime,
    ) -> bool:
        event_key = f"briefing:{user_id}:{local_date.isoformat()}"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO outgoing_notifications
                    (id, event_key, user_id, kind, destination_chat_id, payload,
                     state, attempt_count, scheduled_for, next_attempt_at,
                     trace_id, created_at, updated_at)
                SELECT
                    gen_random_uuid(),
                    $2,
                    s.user_id,
                    'daily_briefing',
                    s.destination_chat_id,
                    jsonb_build_object(
                        'local_date', $3::text,
                        'timezone', $4::text,
                        'city_name', s.city_name,
                        'city_country', s.city_country,
                        'city_region', s.city_region,
                        'city_latitude', s.city_latitude,
                        'city_longitude', s.city_longitude
                    ),
                    'pending',
                    0,
                    $5,
                    $6,
                    $7,
                    $6,
                    $6
                FROM notification_settings s
                WHERE s.user_id = $1
                  AND s.briefing_enabled
                  AND s.briefing_consent_at IS NOT NULL
                  AND s.destination_chat_id IS NOT NULL
                  AND s.city_confirmed_at IS NOT NULL
                  AND s.city_name IS NOT NULL
                  AND s.city_latitude IS NOT NULL
                  AND s.city_longitude IS NOT NULL
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id
                """,
                user_id,
                event_key,
                local_date.isoformat(),
                timezone,
                scheduled_for,
                now,
                trace_id,
            )
        return row is not None

    async def recover_expired_leases(
        self,
        now: datetime,
        max_attempts: int,
    ) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE outgoing_notifications
                SET state = CASE
                        WHEN attempt_count >= $2 THEN 'failed'
                        ELSE 'retry'
                    END,
                    next_attempt_at = $1,
                    lease_id = NULL,
                    lease_until = NULL,
                    last_error_code = 'lease_expired',
                    updated_at = $1
                WHERE state = 'sending'
                  AND lease_until IS NOT NULL
                  AND lease_until <= $1
                """,
                now,
                max_attempts,
            )
        return int(result.split()[-1])

    async def claim_due_event(
        self,
        now: datetime,
        lease_until: datetime,
        max_attempts: int,
    ) -> OutgoingNotificationRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT id
                    FROM outgoing_notifications
                    WHERE state IN ('pending', 'retry')
                      AND next_attempt_at <= $1
                      AND attempt_count < $3
                    ORDER BY next_attempt_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE outgoing_notifications o
                SET state = 'sending',
                    attempt_count = o.attempt_count + 1,
                    lease_id = gen_random_uuid(),
                    lease_until = $2,
                    updated_at = $1
                FROM candidate
                WHERE o.id = candidate.id
                RETURNING o.*
                """,
                now,
                lease_until,
                max_attempts,
            )
        return decode_outgoing(row) if row is not None else None

    async def save_payload(
        self,
        event_id: UUID,
        lease_id: UUID,
        payload: Mapping[str, object],
        now: datetime,
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE outgoing_notifications
                SET payload = $3::jsonb, updated_at = $4
                WHERE id = $1 AND lease_id = $2 AND state = 'sending'
                RETURNING id
                """,
                event_id,
                lease_id,
                json.dumps(dict(payload), ensure_ascii=False),
                now,
            )
        return row is not None

    async def mark_sent(self, event_id: UUID, lease_id: UUID, now: datetime) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE outgoing_notifications
                SET state = 'sent',
                    sent_at = $3,
                    lease_id = NULL,
                    lease_until = NULL,
                    updated_at = $3
                WHERE id = $1 AND lease_id = $2 AND state = 'sending'
                RETURNING id
                """,
                event_id,
                lease_id,
                now,
            )
        return row is not None

    async def mark_retry(
        self,
        event_id: UUID,
        lease_id: UUID,
        next_attempt_at: datetime,
        code: str,
        now: datetime,
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE outgoing_notifications
                SET state = 'retry',
                    next_attempt_at = $3,
                    lease_id = NULL,
                    lease_until = NULL,
                    last_error_code = $4,
                    updated_at = $5
                WHERE id = $1 AND lease_id = $2 AND state = 'sending'
                RETURNING id
                """,
                event_id,
                lease_id,
                next_attempt_at,
                code,
                now,
            )
        return row is not None

    async def mark_terminal(
        self,
        event_id: UUID,
        lease_id: UUID,
        state: NotificationState,
        code: str,
        now: datetime,
    ) -> bool:
        if state not in {
            NotificationState.FAILED,
            NotificationState.MISSED,
            NotificationState.SUPPRESSED,
        }:
            raise ValueError("terminal notification state required")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE outgoing_notifications
                SET state = $3,
                    lease_id = NULL,
                    lease_until = NULL,
                    last_error_code = $4,
                    updated_at = $5
                WHERE id = $1 AND lease_id = $2 AND state = 'sending'
                RETURNING id
                """,
                event_id,
                lease_id,
                state.value,
                code,
                now,
            )
        return row is not None
