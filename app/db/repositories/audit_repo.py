import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

import asyncpg

from app.db.records import DecisionAudit

_SAFE_ARGUMENT_LIMITS = {
    "city": 100,
    "date": 40,
    "text": 200,
    "remind_at": 64,
}


def _safe_arguments(arguments: Mapping[str, object] | None) -> str:
    safe: dict[str, str] = {}
    if isinstance(arguments, Mapping):
        for key, limit in _SAFE_ARGUMENT_LIMITS.items():
            value = arguments.get(key)
            if isinstance(value, str):
                safe[key] = value[:limit]
    return json.dumps(safe, ensure_ascii=False)


async def record_decision(
    pool: asyncpg.Pool,
    *,
    user_id: int,
    event_id: str,
    request_id: str,
    decision: str,
    tool_name: str | None = None,
    arguments: Mapping[str, object] | None = None,
    validation: str = "ok",
    execution: str = "none",
    duration_ms: float = 0,
    reason: str = "",
    created_at: datetime | None = None,
) -> None:
    async with pool.acquire() as conn:
        await record_on_connection(
            conn,
            user_id=user_id,
            event_id=event_id,
            request_id=request_id,
            decision=decision,
            tool_name=tool_name,
            arguments=arguments,
            validation=validation,
            execution=execution,
            duration_ms=duration_ms,
            reason=reason,
            created_at=created_at,
        )


async def record_on_connection(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    user_id: int,
    event_id: str,
    request_id: str,
    decision: str,
    tool_name: str | None = None,
    arguments: Mapping[str, object] | None = None,
    validation: str = "ok",
    execution: str = "none",
    duration_ms: float = 0,
    reason: str = "",
    created_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO decision_audits (
            user_id, event_id, request_id, decision, tool_name, arguments,
            validation, execution, duration_ms, reason, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, COALESCE($11, NOW()))
        """,
        user_id,
        event_id,
        request_id,
        decision,
        tool_name,
        _safe_arguments(arguments),
        validation,
        execution,
        duration_ms,
        reason[:200],
        created_at,
    )


async def latest_decision(pool: asyncpg.Pool, user_id: int) -> DecisionAudit | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, event_id, request_id, decision, tool_name, arguments,
                   validation, execution, duration_ms, reason, created_at
            FROM decision_audits
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            user_id,
        )

    if row is None:
        return None

    result = dict(row)
    if isinstance(result["arguments"], str):
        result["arguments"] = json.loads(result["arguments"])
    return cast(DecisionAudit, result)
