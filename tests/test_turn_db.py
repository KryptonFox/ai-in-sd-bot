import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from conftest import DatabaseFixture

from app.core.clock import FrozenClock
from app.core.schemas.agent import Acquired, AgentDecision, AgentResult, EventContext, Replay
from app.core.schemas.llm import LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import ToolFailure
from app.db.connection import create_pool
from app.db.repositories import confirmation_repo, history_repo, reminder_repo, timezone_repo
from app.db.repositories.turn_repo import PostgresTurnStore
from app.db.repositories.user_repo import get_or_create_user
from app.mcp.client import MCPClient
from app.mcp_server.server import resource_catalog, tool_catalog
from app.services.agent_service import AgentService
from app.services.confirmation_service import ConfirmationService
from app.services.dialog_service import DialogService
from app.services.tool_execution import ToolExecutionService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1", reason="Включите RUN_INTEGRATION=1"
    ),
]
NOW = datetime(2031, 5, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("saved_zone", "expected_zone", "offset_hours"),
    [(None, "Europe/Moscow", 3), ("Asia/Yekaterinburg", "Asia/Yekaterinburg", 5)],
)
async def test_dialog_restores_history_and_effective_timezone_from_database(
    database: DatabaseFixture,
    saved_zone: str | None,
    expected_zone: str,
    offset_hours: int,
) -> None:
    settings = database[0]
    pool = await create_pool(settings)
    clock = FrozenClock(NOW)
    llm = MagicMock(
        get_context_length=AsyncMock(return_value=32768),
        generate=AsyncMock(
            side_effect=[
                LLMResponse(content="Слово запомнил."),
                LLMResponse(content="Ты назвал слово Альфа."),
            ]
        ),
    )
    mcp = MCPClient(settings)
    mcp.load_catalog(tool_catalog())
    mcp.load_resources(resource_catalog())

    def make_service() -> DialogService:
        store = PostgresTurnStore(pool)
        tools = ToolExecutionService(mcp, store=store, clock=clock)
        agent = AgentService(llm, tools, clock=clock)
        return DialogService(
            pool,
            settings,
            llm,
            agent=agent,
            clock=clock,
            tool_client=mcp,
            store=store,
        )

    try:
        first = make_service()
        if saved_zone is not None:
            await first.timezone_status(1, saved_zone)
        await first.process_user_message(1, "Запомни слово Альфа", event_id="remember")

        restarted = make_service()
        status = await restarted.timezone_status(1)
        assert status.zone == expected_zone
        assert status.now.utcoffset() == timedelta(hours=offset_hours)
        assert await timezone_repo.get_timezone(pool, 1) == saved_zone

        await restarted.process_user_message(1, "Какое слово я назвал?", event_id="recall")

        assert llm.generate.await_count == 2
        request = llm.generate.await_args_list[1].args[0]
        assert f"Часовой пояс пользователя (IANA): {expected_zone}." in request.messages[0].content
        conversation = [
            message.content for message in request.messages if message.role != MessageRole.SYSTEM
        ]
        assert conversation == ["Запомни слово Альфа", "Слово запомнил.", "Какое слово я назвал?"]
        assert len(await history_repo.get_user_messages_with_ids(pool, 1)) == 4
    finally:
        await pool.close()


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
            self.pool,
            confirmation_id,
            user_id,
            arguments,
            NOW,
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
        assert result.decision == AgentDecision.TOOL
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
