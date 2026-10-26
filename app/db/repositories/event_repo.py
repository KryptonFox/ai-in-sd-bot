import json
from collections.abc import Mapping
from datetime import datetime

import asyncpg

from app.db.records import EventClaim


async def begin(pool: asyncpg.Pool, user_id: int, event_id: str, now: datetime) -> EventClaim:
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchrow(
            """INSERT INTO agent_events(user_id,event_id,updated_at) VALUES($1,$2,$3)
               ON CONFLICT DO NOTHING RETURNING *""",
            user_id,
            event_id,
            now,
        )
        if inserted:
            return {"claimed": True, "calls_used": 0}
        row = await conn.fetchrow(
            "SELECT * FROM agent_events WHERE user_id=$1 AND event_id=$2 FOR UPDATE",
            user_id,
            event_id,
        )
        assert row is not None

        if row["result"]:
            result = row["result"]
            return {
                "claimed": False,
                "result": json.loads(result) if isinstance(result, str) else result,
            }
        if row["state"] == "failed" or (now - row["updated_at"]).total_seconds() > 600:
            await conn.execute(
                "UPDATE agent_events SET state='processing',updated_at=$3 "
                "WHERE user_id=$1 AND event_id=$2",
                user_id,
                event_id,
                now,
            )
            return {"claimed": True, "calls_used": row["calls_used"]}
        return {"claimed": False, "calls_used": row["calls_used"]}


async def reserve(pool: asyncpg.Pool, user_id: int, event_id: str, limit: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE agent_events SET calls_used=calls_used+1 WHERE user_id=$1 AND event_id=$2
               AND calls_used<$3 RETURNING calls_used""",
            user_id,
            event_id,
            limit,
        )
    return row is not None


async def finish(
    pool: asyncpg.Pool, user_id: int, event_id: str, result: Mapping[str, object], now: datetime
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_events SET state='done',result=$3::jsonb,updated_at=$4 "
            "WHERE user_id=$1 AND event_id=$2",
            user_id,
            event_id,
            json.dumps(dict(result)),
            now,
        )


async def fail(pool: asyncpg.Pool, user_id: int, event_id: str, now: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_events SET state='failed',updated_at=$3 WHERE user_id=$1 AND event_id=$2",
            user_id,
            event_id,
            now,
        )
