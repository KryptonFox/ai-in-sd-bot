import json
import uuid
from datetime import datetime, timedelta

from app.core.schemas.tools import ReminderResult


def decode_record(row) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("arguments", "result"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    result["id"] = str(result["id"])
    return result


async def find_event(pool, user_id: int, event_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pending_confirmations WHERE user_id=$1 AND event_id=$2 "
            "ORDER BY position LIMIT 1",
            user_id,
            event_id,
        )
    return decode_record(row)


async def prepare(
    pool,
    *,
    user_id: int,
    event_id: str,
    position: int,
    arguments: dict,
    timezone: str,
    now: datetime,
    ttl: int,
    calls_remaining: int,
    request_id: str,
) -> dict:
    identifier = uuid.uuid4()
    key = f"reminder:{identifier}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(user_id))
        existing = await conn.fetchrow(
            "SELECT * FROM pending_confirmations WHERE user_id=$1 AND event_id=$2 AND position=$3",
            user_id,
            event_id,
            position,
        )
        if existing:
            return decode_record(existing)
        await conn.execute(
            "UPDATE pending_confirmations SET state='cancelled',updated_at=$2 "
            "WHERE user_id=$1 AND state='pending'",
            user_id,
            now,
        )
        row = await conn.fetchrow(
            """INSERT INTO pending_confirmations
               (id,user_id,event_id,position,arguments,timezone,idempotency_key,
                created_at,expires_at,calls_remaining,request_id)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (user_id,event_id,position) DO UPDATE
               SET event_id=pending_confirmations.event_id RETURNING *""",
            identifier,
            user_id,
            event_id,
            position,
            json.dumps(arguments),
            timezone,
            key,
            now,
            now + timedelta(seconds=ttl),
            calls_remaining,
            request_id,
        )
    return decode_record(row)


async def claim(pool, identifier: str, user_id: int, now: datetime, *, cancel=False) -> dict | None:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM pending_confirmations WHERE id=$1 FOR UPDATE", uuid.UUID(identifier)
        )
        if row is None or row["user_id"] != user_id:
            return None
        if row["state"] == "pending":
            if row["expires_at"] <= now:
                state = "expired"
            elif cancel:
                state = "cancelled"
            elif row["calls_remaining"] <= 0:
                state = "failed"
            else:
                state = "confirmed"
            row = await conn.fetchrow(
                """UPDATE pending_confirmations SET state=$2,updated_at=$3,
                   confirmed_at=CASE WHEN $2='confirmed' THEN $3 ELSE confirmed_at END
                   WHERE id=$1 RETURNING *""",
                uuid.UUID(identifier),
                state,
                now,
            )
        if row["state"] == "confirmed":
            row = await conn.fetchrow(
                """UPDATE pending_confirmations SET state='executing',execution_started_at=$2,
                   updated_at=$2,calls_remaining=calls_remaining-1 WHERE id=$1 RETURNING *""",
                uuid.UUID(identifier),
                now,
            )
            result = decode_record(row)
            result["claimed"] = True
            return result
    result = decode_record(row)
    result["claimed"] = False
    return result


async def complete(pool, identifier: str, result: dict, now: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE pending_confirmations SET state='succeeded',result=$2::jsonb,
               updated_at=$3,error_code=NULL WHERE id=$1 AND state='executing'""",
            uuid.UUID(identifier),
            json.dumps(result),
            now,
        )


async def mark_failed(pool, identifier: str, code: str, now: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE pending_confirmations SET state='failed',error_code=$2,updated_at=$3
               WHERE id=$1 AND state='executing'""",
            uuid.UUID(identifier),
            code,
            now,
        )


async def recoverable(pool, now: datetime) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM pending_confirmations WHERE state IN ('confirmed','executing')
               AND updated_at < $1::timestamptz - INTERVAL '30 seconds'
               ORDER BY created_at LIMIT 100""",
            now,
        )
    return [decode_record(row) for row in rows]


async def recover_claim(pool, identifier: str, now: datetime) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE pending_confirmations SET state='executing',updated_at=$2,
               execution_started_at=COALESCE(execution_started_at,$2),
               calls_remaining=calls_remaining-1
               WHERE id=$1 AND state IN ('confirmed','executing')
               AND updated_at < $2::timestamptz-INTERVAL '30 seconds' AND confirmed_at<=expires_at
               AND calls_remaining>0 RETURNING *""",
            uuid.UUID(identifier),
            now,
        )
    return decode_record(row)


async def finalize_exhausted(pool, identifier: str, now: datetime) -> dict | None:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM pending_confirmations WHERE id=$1 FOR UPDATE", uuid.UUID(identifier)
        )
        if row is None or row["state"] != "executing" or row["calls_remaining"] > 0:
            return None
        reminder = await conn.fetchrow(
            "SELECT * FROM reminders WHERE idempotency_key=$1", row["idempotency_key"]
        )
        result = None
        if reminder is not None:
            result = ReminderResult(
                id=reminder["id"],
                text=reminder["text"],
                remind_at=reminder["remind_at"],
                timezone=reminder["timezone"],
                created=False,
            ).model_dump(mode="json")
        updated = await conn.fetchrow(
            """UPDATE pending_confirmations SET state=$2,result=$3::jsonb,error_code=$4,
               updated_at=$5 WHERE id=$1 RETURNING *""",
            uuid.UUID(identifier),
            "succeeded" if result is not None else "failed",
            json.dumps(result) if result is not None else None,
            None if result is not None else "call_budget",
            now,
        )
    return decode_record(updated)
