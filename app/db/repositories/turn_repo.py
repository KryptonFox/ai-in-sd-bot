import json
import logging
from datetime import datetime

import asyncpg

from app.core.schemas.agent import (
    Acquired,
    AgentResult,
    EventContext,
    InProgress,
    Replay,
    TurnClaim,
)
from app.core.schemas.message import ChatMessage
from app.db.repositories import event_repo
from app.db.repositories.audit_repo import record_decision, record_on_connection

logger = logging.getLogger("app.turns")

INTERRUPTED_TEXT = (
    "Запрос был прерван. Полученные данные не сохранены для продолжения; "
    "отправьте новый запрос. Состояние подтверждённых действий сверяется отдельно."
)


class PostgresTurnStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def claim(self, context: EventContext, now: datetime) -> TurnClaim:
        event = await event_repo.begin(self.pool, context.user_id, context.event_id, now)
        if "result" in event:
            return Replay(AgentResult.model_validate(event["result"]))
        if not event["claimed"]:
            return InProgress()
        used = event.get("calls_used", 0)
        if used:
            await self.interrupt(context, "interrupted", now)
            return Replay(
                AgentResult(text=INTERRUPTED_TEXT, decision="error", reason="interrupted")
            )
        return Acquired(used)

    async def reserve(self, context: EventContext, limit: int) -> bool:
        return await event_repo.reserve(self.pool, context.user_id, context.event_id, limit)

    async def complete(
        self,
        context: EventContext,
        message: ChatMessage,
        assistant: ChatMessage,
        result: AgentResult,
        now: datetime,
        duration_ms: float,
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT state FROM agent_events WHERE user_id=$1 AND event_id=$2 FOR UPDATE",
                context.user_id,
                context.event_id,
            )
            if row is None or row["state"] != "processing":
                raise RuntimeError("turn_not_processing")
            for item in (message, assistant):
                await conn.execute(
                    "INSERT INTO dialog_messages "
                    "(user_id,role,content,char_count,metadata,event_id) "
                    "VALUES($1,$2,$3,$4,$5::jsonb,$6)",
                    context.user_id,
                    item.role.value,
                    item.content,
                    item.char_count,
                    json.dumps(item.metadata),
                    context.event_id,
                )
            await record_on_connection(
                conn,
                user_id=context.user_id,
                event_id=context.event_id,
                request_id=context.request_id,
                decision=result.decision,
                tool_name=result.tool_name,
                arguments=result.arguments,
                validation=result.reason if result.decision in {"error", "clarify"} else "ok",
                execution=result.reason,
                reason=result.reason,
                duration_ms=duration_ms,
                created_at=now,
            )
            await conn.execute(
                "UPDATE agent_events SET state='done', result=$3::jsonb,updated_at=$4 "
                "WHERE user_id=$1 AND event_id=$2",
                context.user_id,
                context.event_id,
                result.model_dump_json(),
                now,
            )

    async def interrupt(self, context: EventContext, reason: str, now: datetime) -> bool:
        result = AgentResult(text=INTERRUPTED_TEXT, decision="error", reason="interrupted")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE agent_events SET state=CASE WHEN calls_used>0 THEN 'interrupted' "
                "ELSE 'failed' END,result=CASE WHEN calls_used>0 THEN $3::jsonb ELSE NULL END,"
                "updated_at=$4 WHERE user_id=$1 AND event_id=$2 AND state='processing' "
                "RETURNING calls_used",
                context.user_id,
                context.event_id,
                result.model_dump_json(),
                now,
            )
        if row is not None:
            try:
                await record_decision(
                    self.pool,
                    user_id=context.user_id,
                    event_id=context.event_id,
                    request_id=context.request_id,
                    decision="error",
                    validation=reason,
                    execution="interrupted",
                    reason=reason,
                    created_at=now,
                )
            except Exception:
                logger.warning("turn_failure_audit_unavailable request=%s", context.request_id)
        return row is not None and row["calls_used"] > 0

    async def recover(self, now: datetime) -> None:
        result = AgentResult(text=INTERRUPTED_TEXT, decision="error", reason="interrupted")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_events SET state='interrupted',result=$1::jsonb,updated_at=$2 "
                "WHERE state='processing'",
                result.model_dump_json(),
                now,
            )
