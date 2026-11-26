from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

from app.core.trace import OperationTrace, TraceStatus, TraceStep

TRACE_RETENTION = timedelta(days=30)


async def save_trace(pool: asyncpg.Pool, trace: OperationTrace) -> None:
    async with pool.acquire() as conn, conn.transaction():
        owner = await conn.fetchval(
            "INSERT INTO operation_traces (id, user_id, request_id, started_at, route) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO UPDATE "
            "SET route = COALESCE(operation_traces.route, EXCLUDED.route) "
            "WHERE operation_traces.user_id = EXCLUDED.user_id RETURNING user_id",
            trace.id,
            trace.user_id,
            trace.request_id,
            trace.started_at,
            trace.route,
        )
        if owner is None:
            return

        first_position = await conn.fetchval(
            "SELECT COALESCE(max(position) + 1, 0) FROM operation_trace_steps WHERE trace_id=$1",
            trace.id,
        )
        assert isinstance(first_position, int)
        await conn.executemany(
            """INSERT INTO operation_trace_steps (
                   id, trace_id, parent_id, position, component, action, started_at, duration_ms,
                   status, tool_name, model_name, input_tokens, output_tokens, error_code
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (id) DO NOTHING""",
            [
                (
                    step.id,
                    trace.id,
                    step.parent_id,
                    position,
                    step.component,
                    step.action,
                    step.started_at,
                    step.duration_ms,
                    step.status,
                    step.tool_name,
                    step.model_name,
                    step.input_tokens,
                    step.output_tokens,
                    step.error_code,
                )
                for position, step in enumerate(trace.steps, start=first_position)
            ],
        )


async def find_trace(
    pool: asyncpg.Pool,
    user_id: int,
    now: datetime,
    trace_id: UUID | None = None,
) -> OperationTrace | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, request_id, started_at, route FROM operation_traces
               WHERE user_id = $1 AND started_at >= $2 AND ($3::uuid IS NULL OR id = $3)
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            user_id,
            now - TRACE_RETENTION,
            trace_id,
        )
        if row is None:
            return None
        steps = await conn.fetch(
            """SELECT id, parent_id, component, action, started_at, duration_ms, status,
                      tool_name, model_name, input_tokens, output_tokens, error_code
               FROM operation_trace_steps WHERE trace_id = $1 ORDER BY position""",
            row["id"],
        )

    return OperationTrace(
        user_id=user_id,
        request_id=row["request_id"],
        started_at=row["started_at"],
        id=row["id"],
        route=row["route"],
        steps=[
            TraceStep(
                component=step["component"],
                action=step["action"],
                started_at=step["started_at"],
                parent_id=step["parent_id"],
                id=step["id"],
                duration_ms=step["duration_ms"],
                status=TraceStatus(step["status"]),
                tool_name=step["tool_name"],
                model_name=step["model_name"],
                input_tokens=step["input_tokens"],
                output_tokens=step["output_tokens"],
                error_code=step["error_code"],
            )
            for step in steps
        ],
    )


async def purge_traces(pool: asyncpg.Pool, now: datetime) -> None:
    await pool.execute("DELETE FROM operation_traces WHERE started_at < $1", now - TRACE_RETENTION)
