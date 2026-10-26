import json
import uuid
from collections.abc import Mapping
from datetime import datetime

import asyncpg

from app.core.schemas.tools import ReminderArguments, ReminderResult, ToolFailure


async def create_confirmed_reminder(
    pool: asyncpg.Pool,
    identifier: str,
    user_id: int,
    arguments: Mapping[str, object],
    now: datetime,
) -> ReminderResult:
    async with pool.acquire() as conn, conn.transaction():
        pending = await conn.fetchrow(
            "SELECT * FROM pending_confirmations WHERE id=$1 FOR UPDATE",
            uuid.UUID(identifier),
        )
        if pending is None or pending["user_id"] != user_id:
            raise ToolFailure("unauthorized", "Действие не разрешено.")
        saved = pending["arguments"]
        if isinstance(saved, str):
            saved = json.loads(saved)
        expected = ReminderArguments.model_validate(saved)
        actual = ReminderArguments.model_validate(arguments)
        if expected != actual or pending["state"] not in {"executing", "succeeded"}:
            raise ToolFailure("unauthorized", "Подтверждение не соответствует действию.")
        if pending["confirmed_at"] is None or pending["confirmed_at"] > pending["expires_at"]:
            raise ToolFailure("expired", "Подтверждение истекло.")
        existing = await conn.fetchrow(
            "SELECT * FROM reminders WHERE idempotency_key=$1",
            pending["idempotency_key"],
        )
        created = False
        if existing is None:
            if actual.remind_at <= now:
                raise ToolFailure("past_time", "Время напоминания уже прошло.")
            existing = await conn.fetchrow(
                """INSERT INTO reminders(user_id,text,remind_at,timezone,idempotency_key)
                   VALUES ($1,$2,$3,$4,$5) ON CONFLICT (idempotency_key) DO UPDATE
                   SET idempotency_key=reminders.idempotency_key RETURNING *""",
                user_id,
                actual.text,
                actual.remind_at,
                pending["timezone"],
                pending["idempotency_key"],
            )
            created = True
        assert existing is not None

        return ReminderResult(
            id=existing["id"],
            text=existing["text"],
            remind_at=existing["remind_at"],
            timezone=existing["timezone"],
            created=created,
        )


async def find_result(pool: asyncpg.Pool, key: str) -> dict[str, object] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM reminders WHERE idempotency_key=$1", key)
    if row is None:
        return None
    return ReminderResult(
        id=row["id"],
        text=row["text"],
        remind_at=row["remind_at"],
        timezone=row["timezone"],
        created=False,
    ).model_dump(mode="json")
