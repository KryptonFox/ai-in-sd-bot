import asyncio
import json
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from conftest import DatabaseFixture

from app.core.clock import FrozenClock
from app.core.schemas.agent import EventContext
from app.core.schemas.tools import ToolFailure
from app.db.connection import create_pool
from app.db.repositories import confirmation_repo, event_repo, reminder_repo
from app.db.repositories.audit_repo import latest_decision
from app.services.confirmation_service import ConfirmationService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]


class InsertThenTimeoutMCP:
    def __init__(self, pool, clock, *, fail_after_insert):
        self.pool = pool
        self.clock = clock
        self.fail_after_insert = fail_after_insert
        self.calls = 0

    async def call_tool(self, name, arguments, *, user_id=None, confirmation_id=None):
        assert user_id is not None
        assert confirmation_id is not None
        self.calls += 1
        assert name == "add_reminder"
        result = await reminder_repo.create_confirmed_reminder(
            self.pool,
            confirmation_id,
            user_id,
            arguments,
            self.clock.now(),
        )
        if self.fail_after_insert:
            raise ToolFailure("mcp_timeout", "response lost")
        return result.model_dump(mode="json")


class TimeoutBeforeInsertMCP:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, arguments, *, user_id=None, confirmation_id=None):
        assert user_id is not None
        assert confirmation_id is not None
        self.calls += 1
        assert name == "add_reminder"
        raise ToolFailure("mcp_timeout", "response lost")


def _context(user_id: int, event_id: str) -> EventContext:
    return EventContext(
        user_id=user_id,
        event_id=event_id,
        request_id=f"request:{event_id}",
    )


def _arguments(now: datetime, text: str = "проверить расписание") -> dict[str, str]:
    return {
        "text": text,
        "remind_at": (now + timedelta(days=2)).isoformat(),
    }


async def test_confirmation_prepare_claim_and_reminder_idempotency(database: DatabaseFixture):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2031, 5, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    clock = FrozenClock(now)
    mcp = InsertThenTimeoutMCP(pool, clock, fail_after_insert=False)
    service = ConfirmationService(pool, mcp, clock=clock, ttl=90)
    context = _context(901, "eval:prepare-concurrency")
    original = _arguments(now)
    changed = _arguments(now, "удалить все данные")

    try:
        first, second = await asyncio.gather(
            service.prepare(context, original, "Europe/Moscow", position=0, calls_remaining=2),
            service.prepare(context, original, "Europe/Moscow", position=0, calls_remaining=2),
        )
        row_before = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert row_before is not None
        later_service = ConfirmationService(
            pool,
            mcp,
            clock=FrozenClock(now + timedelta(seconds=20)),
            ttl=900,
        )
        repeated = await later_service.prepare(
            context,
            changed,
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert repeated.confirmation_id is not None
        row_after = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert row_after is not None

        assert first.confirmation_id == second.confirmation_id == repeated.confirmation_id
        assert row_after["arguments"] == original
        assert row_after["created_at"] == row_before["created_at"]
        assert row_after["expires_at"] == row_before["expires_at"]
        assert row_after["expires_at"] - row_after["created_at"] == timedelta(seconds=90)
        assert (
            await confirmation_repo.claim(pool, row_after["id"], context.user_id + 1, now) is None
        )
        assert await service.existing(_context(context.user_id + 1, context.event_id)) is None
        denied = await service.confirm(context.user_id + 1, row_after["id"])
        denied_audit = await latest_decision(pool, context.user_id + 1)
        assert denied_audit is not None
        assert denied.decision == "error"
        assert denied_audit["event_id"] == "confirmation:denied"
        assert denied_audit["execution"] == "denied"

        expired_context = _context(901, "eval:expired")
        expired_result = await service.prepare(
            expired_context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert expired_result.confirmation_id is not None
        expired_service = ConfirmationService(
            pool,
            mcp,
            clock=FrozenClock(now + timedelta(seconds=91)),
            ttl=90,
        )
        expired_result_status = await expired_service.confirm(
            expired_context.user_id,
            expired_result.confirmation_id,
        )
        expired_row = await confirmation_repo.find_event(
            pool,
            expired_context.user_id,
            expired_context.event_id,
        )
        assert expired_row is not None
        expired_audit = await latest_decision(pool, expired_context.user_id)
        assert expired_audit is not None
        assert expired_result_status.decision == "respond"
        assert expired_row["state"] == "expired"
        assert expired_audit["event_id"] == expired_context.event_id
        assert expired_audit["execution"] == "expired"
        assert expired_audit["reason"] == "confirmation_state"

        cancelled_context = _context(901, "eval:cancelled")
        cancelled_result = await service.prepare(
            cancelled_context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert cancelled_result.confirmation_id is not None
        cancelled_result_status = await service.confirm(
            cancelled_context.user_id,
            cancelled_result.confirmation_id,
            cancel=True,
        )
        cancelled_row = await confirmation_repo.find_event(
            pool,
            cancelled_context.user_id,
            cancelled_context.event_id,
        )
        assert cancelled_row is not None
        cancelled_audit = await latest_decision(pool, cancelled_context.user_id)
        assert cancelled_audit is not None
        assert cancelled_result_status.decision == "respond"
        assert cancelled_row["state"] == "cancelled"
        assert cancelled_audit["event_id"] == cancelled_context.event_id
        assert cancelled_audit["execution"] == "cancelled"
        assert cancelled_audit["reason"] == "confirmation_state"

        confirm_context = _context(902, "eval:confirm-race")
        prepared = await service.prepare(
            confirm_context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert prepared.confirmation_id is not None
        outcomes = await asyncio.gather(
            service.confirm(confirm_context.user_id, prepared.confirmation_id),
            service.confirm(confirm_context.user_id, prepared.confirmation_id),
        )
        pending = await confirmation_repo.find_event(
            pool, confirm_context.user_id, confirm_context.event_id
        )
        assert pending is not None

        assert mcp.calls == 1
        assert pending["state"] == "succeeded"
        assert pending["calls_remaining"] == 1
        assert {result.decision for result in outcomes} <= {"tool", "respond"}
        assert any(result.decision == "tool" for result in outcomes)
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 902) == 1

        direct_result = await reminder_repo.create_confirmed_reminder(
            pool,
            prepared.confirmation_id,
            confirm_context.user_id,
            pending["arguments"],
            now,
        )
        assert direct_result.created is False
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 902) == 1
        with pytest.raises(ToolFailure) as wrong_owner:
            await reminder_repo.create_confirmed_reminder(
                pool,
                prepared.confirmation_id,
                confirm_context.user_id + 1,
                pending["arguments"],
                now,
            )
        assert wrong_owner.value.code == "unauthorized"
        with pytest.raises(ToolFailure) as changed_payload:
            await reminder_repo.create_confirmed_reminder(
                pool,
                prepared.confirmation_id,
                confirm_context.user_id,
                _arguments(now, "другое действие"),
                now,
            )
        assert changed_payload.value.code == "unauthorized"

        audit = await latest_decision(pool, confirm_context.user_id)
        assert audit is not None
        assert audit["event_id"] == confirm_context.event_id
        assert audit["execution"] == "succeeded"
        assert set(audit["arguments"]) == {"text", "remind_at"}
        assert json.dumps(audit["arguments"], ensure_ascii=False).find("удалить все данные") == -1
    finally:
        await pool.close()


async def test_lost_mcp_response_recovers_from_committed_reminder(database: DatabaseFixture):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2032, 6, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    clock = FrozenClock(now)
    mcp = InsertThenTimeoutMCP(pool, clock, fail_after_insert=True)
    service = ConfirmationService(pool, mcp, clock=clock, ttl=120)
    context = _context(903, "eval:lost-response")

    try:
        prepared = await service.prepare(
            context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert prepared.confirmation_id is not None
        result = await service.confirm(context.user_id, prepared.confirmation_id)
        pending = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert pending is not None

        assert result.decision == "tool"
        assert pending["state"] == "succeeded"
        assert pending["calls_remaining"] == 1
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 903) == 1

        service.clock = FrozenClock(now + timedelta(seconds=31))
        mcp.clock = service.clock
        await service.recover()
        recovered = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert recovered is not None
        assert recovered["result"] is not None

        assert mcp.calls == 1
        assert recovered["state"] == "succeeded"
        assert recovered["calls_remaining"] == 1
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 903) == 1
        audit = await latest_decision(pool, context.user_id)
        assert audit is not None
        assert audit["event_id"] == context.event_id
        assert audit["execution"] == "succeeded"
        assert audit["reason"] == "recovered_committed_result"
    finally:
        await pool.close()


async def test_event_retry_preserves_call_budget_and_caches_result(database: DatabaseFixture):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2033, 7, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    user_id = 904
    event_id = "eval:event-budget"
    payload = {"text": "готово", "decision": "respond"}

    try:
        first = await event_repo.begin(pool, user_id, event_id, now)
        duplicate_in_progress = await event_repo.begin(
            pool, user_id, event_id, now + timedelta(seconds=1)
        )

        assert first == {"claimed": True, "calls_used": 0}
        assert duplicate_in_progress == {"claimed": False, "calls_used": 0}
        assert await event_repo.reserve(pool, user_id, event_id, 2) is True
        assert await event_repo.reserve(pool, user_id, event_id, 2) is True
        assert await event_repo.reserve(pool, user_id, event_id, 2) is False

        await event_repo.fail(pool, user_id, event_id, now + timedelta(seconds=2))
        retry = await event_repo.begin(pool, user_id, event_id, now + timedelta(seconds=3))

        assert retry == {"claimed": True, "calls_used": 2}
        assert await event_repo.reserve(pool, user_id, event_id, 2) is False

        finished_at = now + timedelta(seconds=4)
        await event_repo.finish(pool, user_id, event_id, payload, finished_at)
        cached = await event_repo.begin(pool, user_id, event_id, now + timedelta(seconds=5))
        row = await pool.fetchrow(
            "SELECT state,calls_used,result,updated_at FROM agent_events "
            "WHERE user_id=$1 AND event_id=$2",
            user_id,
            event_id,
        )
        assert row is not None
        stored_result = row["result"]
        if isinstance(stored_result, str):
            stored_result = json.loads(stored_result)

        assert cached == {"claimed": False, "result": payload}
        assert row["state"] == "done"
        assert row["calls_used"] == 2
        assert stored_result == payload
        assert row["updated_at"] == finished_at
    finally:
        await pool.close()


async def test_new_pending_cancels_old_but_replay_does_not_cancel_other_pending(
    database: DatabaseFixture,
):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2034, 8, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    mcp = InsertThenTimeoutMCP(pool, FrozenClock(now), fail_after_insert=False)
    first_service = ConfirmationService(pool, mcp, clock=FrozenClock(now), ttl=60)
    next_service = ConfirmationService(
        pool,
        mcp,
        clock=FrozenClock(now + timedelta(seconds=10)),
        ttl=120,
    )
    replay_service = ConfirmationService(
        pool,
        mcp,
        clock=FrozenClock(now + timedelta(seconds=20)),
        ttl=900,
    )
    old_context = _context(905, "eval:old-pending")
    new_context = _context(905, "eval:new-pending")

    try:
        old_result = await first_service.prepare(
            old_context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert old_result.confirmation_id is not None
        old_before = await confirmation_repo.find_event(pool, 905, old_context.event_id)
        assert old_before is not None
        new_result = await next_service.prepare(
            new_context,
            _arguments(now + timedelta(seconds=10), "новое напоминание"),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert new_result.confirmation_id is not None
        replayed = await replay_service.prepare(
            old_context,
            _arguments(now + timedelta(seconds=20), "изменённый повтор"),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        old_after = await confirmation_repo.find_event(pool, 905, old_context.event_id)
        assert old_after is not None
        new_after = await confirmation_repo.find_event(pool, 905, new_context.event_id)
        assert new_after is not None

        assert old_before["state"] == "pending"
        assert old_after["state"] == "cancelled"
        assert old_after["id"] == old_result.confirmation_id
        assert old_after["arguments"] == _arguments(now)
        assert old_after["created_at"] == old_before["created_at"]
        assert old_after["expires_at"] == old_before["expires_at"]
        assert new_after["state"] == "pending"
        assert new_after["id"] == new_result.confirmation_id
        assert new_after["expires_at"] - new_after["created_at"] == timedelta(seconds=120)
        assert replayed.decision == "respond"
    finally:
        await pool.close()


async def test_recovery_exhausts_remaining_calls_without_third_rpc(database: DatabaseFixture):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2035, 9, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    clock = FrozenClock(now)
    mcp = TimeoutBeforeInsertMCP()
    service = ConfirmationService(pool, mcp, clock=clock, ttl=120)
    context = _context(906, "eval:recovery-budget")

    try:
        prepared = await service.prepare(
            context,
            _arguments(now),
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert prepared.confirmation_id is not None
        first_result = await service.confirm(context.user_id, prepared.confirmation_id)
        after_first = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert after_first is not None

        assert first_result.decision == "error"
        assert mcp.calls == 1
        assert after_first["state"] == "executing"
        assert after_first["calls_remaining"] == 1
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 906) == 0

        service.clock = FrozenClock(now + timedelta(seconds=31))
        await service.recover()
        after_retry = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert after_retry is not None

        assert mcp.calls == 2
        assert after_retry["state"] == "executing"
        assert after_retry["calls_remaining"] == 0
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 906) == 0

        service.clock = FrozenClock(now + timedelta(seconds=62))
        await service.recover()
        await service.recover()
        failed = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert failed is not None
        audit = await latest_decision(pool, context.user_id)
        assert audit is not None

        assert mcp.calls == 2
        assert failed["state"] == "failed"
        assert failed["calls_remaining"] == 0
        assert failed["error_code"] == "call_budget"
        assert audit["event_id"] == context.event_id
        assert audit["execution"] == "failed"
        assert audit["reason"] == "call_budget"
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", 906) == 0
    finally:
        await pool.close()


async def test_exhausted_recovery_waits_for_inflight_commit(database: DatabaseFixture):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2036, 9, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    mcp = AsyncMock()
    service = ConfirmationService(pool, mcp, clock=FrozenClock(now), ttl=120)
    context = _context(907, "eval:late-commit")
    recovery = None
    try:
        proposal = await service.prepare(
            context, _arguments(now), "Europe/Moscow", position=0, calls_remaining=1
        )
        assert proposal.confirmation_id is not None
        row = await confirmation_repo.claim(pool, proposal.confirmation_id, context.user_id, now)
        assert row is not None
        assert row["calls_remaining"] == 0
        service.clock = FrozenClock(now + timedelta(seconds=31))
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchrow(
                "SELECT * FROM pending_confirmations WHERE id=$1 FOR UPDATE", row["id"]
            )
            locker = await conn.fetchval("SELECT pg_backend_pid()")
            recovery = asyncio.create_task(service.recover())
            for _ in range(100):
                blocked = await pool.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE $1=ANY(pg_blocking_pids(pid)))",
                    locker,
                )
                if blocked:
                    break
                await asyncio.sleep(0.02)
            assert blocked
            await conn.execute(
                """INSERT INTO reminders(user_id,text,remind_at,timezone,idempotency_key)
                   VALUES($1,$2,$3,$4,$5)""",
                context.user_id,
                row["arguments"]["text"],
                datetime.fromisoformat(row["arguments"]["remind_at"]),
                row["timezone"],
                row["idempotency_key"],
            )
        await asyncio.wait_for(recovery, timeout=5)
        recovered = await confirmation_repo.find_event(pool, context.user_id, context.event_id)
        assert recovered is not None
        assert recovered["result"] is not None
        assert recovered["state"] == "succeeded"
        assert recovered["result"]["created"] is False
        assert recovered["calls_remaining"] == 0
        mcp.call_tool.assert_not_awaited()
    finally:
        if recovery and not recovery.done():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await pool.close()
