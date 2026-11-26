from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from app.db.notification_records import (
    CitySelectionRecord,
    CitySelectionState,
    NotificationSettingsRecord,
)
from app.db.repositories.notification_common import (
    SettingsReaderRepository,
    decode_city_selection,
    fetch_settings,
)


class PostgresNotificationPreferencesRepository(SettingsReaderRepository):
    async def remember_private_chat(
        self,
        user_id: int,
        chat_id: int,
        now: datetime,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notification_settings
                    (user_id, destination_chat_id, created_at, updated_at)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (user_id) DO UPDATE
                SET destination_chat_id = EXCLUDED.destination_chat_id,
                    updated_at = EXCLUDED.updated_at
                """,
                user_id,
                chat_id,
                now,
            )

    async def create_city_selection(self, selection: CitySelectionRecord) -> None:
        candidates = [asdict(candidate) for candidate in selection.candidates]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE notification_city_selections
                SET state = 'cancelled', updated_at = $2
                WHERE user_id = $1 AND state = 'pending'
                """,
                selection.user_id,
                selection.created_at,
            )
            await conn.execute(
                """
                INSERT INTO notification_city_selections
                    (id, user_id, candidates, state, created_at, expires_at, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $5)
                """,
                selection.id,
                selection.user_id,
                json.dumps(candidates, ensure_ascii=False),
                selection.state.value,
                selection.created_at,
                selection.expires_at,
            )

    async def city_selection(self, selection_id: UUID) -> CitySelectionRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM notification_city_selections WHERE id = $1",
                selection_id,
            )
        return decode_city_selection(row) if row is not None else None

    async def confirm_city_selection(
        self,
        selection_id: UUID,
        user_id: int,
        index: int,
        now: datetime,
    ) -> NotificationSettingsRecord | None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM notification_city_selections WHERE id = $1 FOR UPDATE",
                selection_id,
            )
            if row is None or row["user_id"] != user_id:
                return None

            selection = decode_city_selection(row)
            if selection.state is not CitySelectionState.PENDING:
                return None

            if selection.expires_at <= now:
                await conn.execute(
                    """
                    UPDATE notification_city_selections
                    SET state = 'expired', updated_at = $2
                    WHERE id = $1 AND state = 'pending'
                    """,
                    selection_id,
                    now,
                )
                return None

            if index < 0 or index >= len(selection.candidates):
                return None

            city = selection.candidates[index]
            await conn.execute(
                """
                UPDATE notification_city_selections
                SET state = 'confirmed', selected_index = $2, confirmed_at = $3, updated_at = $3
                WHERE id = $1 AND state = 'pending'
                """,
                selection_id,
                index,
                now,
            )
            await conn.execute(
                """
                INSERT INTO notification_settings
                    (user_id, city_name, city_country, city_region, city_latitude,
                     city_longitude, city_confirmed_at, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $7)
                ON CONFLICT (user_id) DO UPDATE
                SET city_name = EXCLUDED.city_name,
                    city_country = EXCLUDED.city_country,
                    city_region = EXCLUDED.city_region,
                    city_latitude = EXCLUDED.city_latitude,
                    city_longitude = EXCLUDED.city_longitude,
                    city_confirmed_at = EXCLUDED.city_confirmed_at,
                    updated_at = EXCLUDED.updated_at
                """,
                user_id,
                city.name,
                city.country,
                city.region,
                city.latitude,
                city.longitude,
                now,
            )
            return await fetch_settings(conn, user_id)

    async def cancel_city_selection(
        self,
        selection_id: UUID,
        user_id: int,
        now: datetime,
    ) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE notification_city_selections
                SET state = 'cancelled', updated_at = $3
                WHERE id = $1 AND user_id = $2 AND state = 'pending'
                """,
                selection_id,
                user_id,
                now,
            )
        return result == "UPDATE 1"

    async def set_briefing_enabled(
        self,
        user_id: int,
        enabled: bool,
        now: datetime,
    ) -> NotificationSettingsRecord | None:
        async with self._pool.acquire() as conn, conn.transaction():
            if enabled:
                result = await conn.execute(
                    """
                    UPDATE notification_settings
                    SET briefing_enabled = TRUE,
                        briefing_consent_at = $2,
                        updated_at = $2
                    WHERE user_id = $1
                      AND destination_chat_id IS NOT NULL
                      AND city_confirmed_at IS NOT NULL
                    """,
                    user_id,
                    now,
                )
                if result != "UPDATE 1":
                    return None
            else:
                await conn.execute(
                    """
                    INSERT INTO notification_settings
                        (user_id, briefing_enabled, created_at, updated_at)
                    VALUES ($1, FALSE, $2, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET briefing_enabled = FALSE,
                        briefing_consent_at = NULL,
                        updated_at = EXCLUDED.updated_at
                    """,
                    user_id,
                    now,
                )

            return await fetch_settings(conn, user_id)
