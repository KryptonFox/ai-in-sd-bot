import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from conftest import DatabaseFixture

from app.core.clock import FrozenClock
from app.core.schemas.agent import Acquired, AgentResult, EventContext, Replay
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import ToolFailure
from app.db.connection import create_pool
from app.db.repositories import confirmation_repo, history_repo, reminder_repo
from app.db.repositories.turn_repo import PostgresTurnStore
from app.db.repositories.user_repo import get_or_create_user
from app.services.confirmation_service import ConfirmationService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1", reason="Включите RUN_INTEGRATION=1"
    ),
]
NOW = datetime(2031, 5, 1, 12, tzinfo=UTC)


async def install_failure(pool: asyncpg.Pool, table: str) -> None:
    assert table in {"dialog_messages", "dialog_summaries", "agent_events"}
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE OR REPLACE FUNCTION fail_write() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'injected'; END; $$ LANGUAGE plpgsql"
        )
        await conn.execute(
            f"CREATE TRIGGER injected BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION fail_write()"
        )


async def remove_failure(pool: asyncpg.Pool, table: str) -> None:
    assert table in {"dialog_messages", "dialog_summaries", "agent_events"}
    async with pool.acquire() as conn:
        await conn.execute(f"DROP TRIGGER injected ON {table}")


async def test_real_compaction_and_reset_roll_back_all_changes(database: DatabaseFixture) -> None:
    pool = await create_pool(database[0])
    try:
        await get_or_create_user(pool, 1)
        for role in (MessageRole.USER, MessageRole.ASSISTANT):
            await history_repo.add_dialog_message(pool, 1, ChatMessage(role=role, content="keep"))
        rows = await history_repo.get_user_messages_with_ids(pool, 1)
        ids = [identifier for identifier, _ in rows]
        for table in ("dialog_summaries", "dialog_messages"):
            await install_failure(pool, table)
            with pytest.raises(asyncpg.RaiseError):
                await history_repo.commit_compaction(pool, 1, ids, "new summary")
            await remove_failure(pool, table)
            assert len(await history_repo.get_user_messages_with_ids(pool, 1)) == 2
            assert await history_repo.get_dialog_summary(pool, 1) is None
        await history_repo.set_dialog_summary(pool, 1, "old summary")
        await install_failure(pool, "dialog_summaries")
        with pytest.raises(asyncpg.RaiseError):
            await history_repo.clear_dialog_history(pool, 1)
        await remove_failure(pool, "dialog_summaries")
        assert len(await history_repo.get_user_messages_with_ids(pool, 1)) == 2
        assert await history_repo.get_dialog_summary(pool, 1) == "old summary"
    finally:
        await pool.close()


async def test_real_turn_commit_is_atomic_and_replay_does_not_add_messages(
    database: DatabaseFixture,
) -> None:
    pool = await create_pool(database[0])
    store = PostgresTurnStore(pool)
    context = EventContext(user_id=1, event_id="same", request_id="test")
    user = ChatMessage(role=MessageRole.USER, content="question")
    assistant = ChatMessage(role=MessageRole.ASSISTANT, content="answer")
    result = AgentResult(text="answer")
    try:
        await get_or_create_user(pool, 1)
        assert isinstance(await store.claim(context, NOW), Acquired)
        await install_failure(pool, "agent_events")
        with pytest.raises(asyncpg.RaiseError):
            await store.complete(context, user, assistant, result, NOW, 10)
        await remove_failure(pool, "agent_events")
        assert not await history_repo.get_user_messages_with_ids(pool, 1)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM decision_audits") == 0
        await store.complete(context, user, assistant, result, NOW, 10)
        replay = await store.claim(context, NOW)
        assert isinstance(replay, Replay) and replay.result.text == "answer"
        with pytest.raises(RuntimeError, match="turn_not_processing"):
            await store.complete(context, user, assistant, result, NOW, 10)
        assert len(await history_repo.get_user_messages_with_ids(pool, 1)) == 2
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM decision_audits") == 1
    finally:
        await pool.close()


class CommitThenInvalidOutput:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.calls = 0

    async def call_tool(self, name, arguments, *, user_id=None, confirmation_id=None):
        self.calls += 1
        assert user_id is not None and confirmation_id is not None
        await reminder_repo.create_confirmed_reminder(
            self.pool, confirmation_id, user_id, arguments, NOW
        )
        raise ToolFailure("invalid_output", "response could not be validated")


async def test_committed_mutation_with_invalid_output_reconciles_without_new_rpc(
    database: DatabaseFixture,
) -> None:
    pool = await create_pool(database[0])
    tool = CommitThenInvalidOutput(pool)
    service = ConfirmationService(pool, tool, FrozenClock(NOW))
    context = EventContext(user_id=1, event_id="reminder", request_id="test")
    try:
        prepared = await service.prepare(
            context,
            {"text": "test", "remind_at": (NOW + timedelta(days=1)).isoformat()},
            "UTC",
            position=0,
            calls_remaining=2,
        )
        assert prepared.confirmation_id is not None
        result = await service.confirm(1, prepared.confirmation_id)
        assert result.decision == "tool"
        assert tool.calls == 1
        row = await confirmation_repo.find_event(pool, 1, "reminder")
        assert row is not None and row["state"] == "succeeded"
        await service.confirm(1, prepared.confirmation_id)
        assert tool.calls == 1
        action = await service.action_context(1)
        assert action is not None and '"state": "succeeded"' in action.content
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM reminders") == 1
    finally:
        await pool.close()
