import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from app.config import Settings
from app.core.budget import ContextBudget
from app.core.clock import FrozenClock
from app.core.exceptions import InterruptedRunError, LLMTimeoutError
from app.core.queue import UserQueueManager
from app.core.schemas.agent import AgentResult, EventContext, InProgress
from app.core.schemas.llm import LLMResponse, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.mcp.client import MCPClient
from app.mcp_server.server import tool_catalog
from app.services.agent_service import AgentService
from app.services.dialog_service import DialogService
from app.services.tool_execution import ToolExecutionService
from tests.fake_turns import MemoryHistory, MemoryTurn, MemoryTurnStore
from tests.test_bot_scenarios import InMemoryPool, _InMemoryConnectionContext, make_dialog

NOW = datetime(2026, 10, 27, 9, tzinfo=UTC)
SETTINGS = Settings(bot_token="unused", postgres_password="unused")


class MockTools(MCPClient):
    call_tool: AsyncMock


async def test_replay_precedes_compaction_and_writes_one_turn() -> None:
    pool = InMemoryPool()
    llm = MagicMock(generate=AsyncMock(return_value=LLMResponse(content="answer")))
    service = make_dialog(pool, SETTINGS, llm)
    first = await service.process_user_message(1, "question", event_id="same")
    second = await service.process_user_message(1, "question", event_id="same")
    assert first == second
    assert len(pool.messages) == 2
    llm.generate.assert_awaited_once()


async def test_direct_calls_without_event_context_have_distinct_keys() -> None:
    pool = InMemoryPool()
    llm = MagicMock(generate=AsyncMock(return_value=LLMResponse(content="answer")))
    service = make_dialog(pool, SETTINGS, llm)
    await service.process_user_message(1, "first")
    await service.process_user_message(1, "second")
    assert len({item["event_id"] for item in pool.messages}) == 2
    assert len(pool.messages) == 4


async def test_in_progress_is_not_an_assistant_message() -> None:
    pool = InMemoryPool()
    store = MemoryTurnStore(cast(MemoryHistory, pool))
    store.turns[1, "busy"] = MemoryTurn()
    llm = MagicMock(generate=AsyncMock())
    service = DialogService(cast(asyncpg.Pool, pool), SETTINGS, llm, store=store)
    result = await service.process_user_message(1, "question", event_id="busy")
    assert isinstance(result, InProgress)
    assert not pool.messages
    llm.generate.assert_not_awaited()


@pytest.mark.parametrize("failed_step", ["summary", "delete"])
async def test_compaction_storage_failure_rolls_back(
    failed_step: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = InMemoryPool()
    for index in range(10):
        pool.messages.append(
            {
                "id": index + 1,
                "user_id": 1,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": str(index),
                "char_count": 1,
                "metadata": {},
            }
        )
    pool.summaries[1] = "previous"
    original = _InMemoryConnectionContext.execute

    async def fail(conn, query, *args):
        if (failed_step == "summary" and "INSERT INTO dialog_summaries" in query) or (
            failed_step == "delete" and "id=ANY" in query
        ):
            raise RuntimeError("injected_storage_failure")
        return await original(conn, query, *args)

    monkeypatch.setattr(_InMemoryConnectionContext, "execute", fail)
    llm = MagicMock(generate=AsyncMock(return_value=LLMResponse(content="new summary")))
    service = make_dialog(pool, SETTINGS, llm)
    with pytest.raises(RuntimeError, match="injected_storage_failure"):
        await service.compact_history(1)
    assert len(pool.messages) == 10
    assert pool.summaries[1] == "previous"


async def test_reset_waits_for_compaction_and_does_not_resurrect_summary() -> None:
    pool = InMemoryPool()
    for index in range(10):
        pool.messages.append(
            {
                "id": index + 1,
                "user_id": 1,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": str(index),
                "char_count": 1,
                "metadata": {},
            }
        )
    entered, release = asyncio.Event(), asyncio.Event()

    async def summarize(request):
        entered.set()
        await release.wait()
        return LLMResponse(content="summary")

    service = make_dialog(pool, SETTINGS, MagicMock(generate=AsyncMock(side_effect=summarize)))
    compact = asyncio.create_task(service.compact_history(1))
    await entered.wait()
    reset = asyncio.create_task(service.reset_history(1))
    await asyncio.sleep(0)
    assert not reset.done()
    release.set()
    await asyncio.gather(compact, reset)
    assert not pool.messages and not pool.summaries


async def test_cancelled_waiter_releases_registration() -> None:
    queue = UserQueueManager()
    async with queue.acquire(1):

        async def wait():
            async with queue.acquire(1):
                raise AssertionError("cancelled waiter entered")

        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert await queue.get_queue_size(1) == 1
    assert not await queue.is_user_busy(1)
    async with queue.acquire(1):
        assert await queue.get_queue_size(1) == 1


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(content="plain"),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="finish",
                    name="finish_response",
                    arguments={"action": "respond", "text": "final"},
                )
            ],
        ),
        LLMResponse(content="", tool_calls=[ToolCall(id="invalid", name="missing", arguments={})]),
        LLMTimeoutError("timeout"),
    ],
)
async def test_dry_run_does_not_touch_store_on_any_outcome(response) -> None:
    store = MagicMock(
        reserve=AsyncMock(), complete=AsyncMock(), interrupt=AsyncMock(), claim=AsyncMock()
    )
    llm = MagicMock(
        generate=AsyncMock(side_effect=response)
        if isinstance(response, Exception)
        else AsyncMock(return_value=response)
    )
    agent = AgentService(llm, ToolExecutionService(store=store))
    if isinstance(response, Exception):
        with pytest.raises(LLMTimeoutError):
            await agent.run(
                [], EventContext(user_id=1, event_id="real", request_id="request"), dry_run=True
            )
    else:
        await agent.run(
            [], EventContext(user_id=1, event_id="real", request_id="request"), dry_run=True
        )
    for method in (store.claim, store.reserve, store.complete, store.interrupt):
        method.assert_not_awaited()


async def test_truncated_tool_call_never_dispatches() -> None:
    mcp = MockTools(SETTINGS)
    mcp.load_catalog(tool_catalog())
    call = AsyncMock()
    mcp.call_tool = call
    llm = MagicMock(
        generate=AsyncMock(
            return_value=LLMResponse(
                content="partial",
                finish_reason="length",
                tool_calls=[
                    ToolCall(id="weather", name="get_weather", arguments={"city": "Казань"})
                ],
            )
        )
    )
    result = await AgentService(llm, ToolExecutionService(mcp)).run(
        [], EventContext(user_id=1, event_id="e", request_id="r")
    )
    assert result.reason == "truncated_tool_call"
    call.assert_not_awaited()


async def test_partial_read_then_timeout_requires_new_turn_without_repeating_rpc() -> None:
    pool = InMemoryPool()
    store = MemoryTurnStore(cast(MemoryHistory, pool))
    clock = FrozenClock(NOW)
    mcp = MockTools(SETTINGS)
    mcp.load_catalog(tool_catalog())
    call = AsyncMock(return_value={"status": "ok"})
    mcp.call_tool = call
    llm = MagicMock(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="weather", name="get_weather", arguments={"city": "Казань"})
                    ],
                ),
                LLMTimeoutError("timeout"),
            ]
        )
    )
    tools = ToolExecutionService(mcp, store=store, clock=clock)
    agent = AgentService(llm, tools, clock=clock)
    service = DialogService(
        cast(asyncpg.Pool, pool), SETTINGS, llm, agent=agent, tools=tools, store=store
    )
    with pytest.raises(InterruptedRunError):
        await service.process_user_message(1, "weather", event_id="partial")
    replay = await service.process_user_message(1, "weather", event_id="partial")
    assert isinstance(replay, AgentResult) and replay.reason == "interrupted"
    assert store.turns[1, "partial"].calls == 1
    call.assert_awaited_once()
    assert not pool.messages


async def test_startup_marks_unfinished_turns_as_interrupted() -> None:
    pool = InMemoryPool()
    store = MemoryTurnStore(cast(MemoryHistory, pool))
    store.turns[1, "old"] = MemoryTurn()
    llm = MagicMock(generate=AsyncMock())
    service = DialogService(cast(asyncpg.Pool, pool), SETTINGS, llm, store=store)
    await service.recover()
    result = await service.process_user_message(1, "retry", event_id="old")
    assert isinstance(result, AgentResult) and result.reason == "interrupted"
    llm.generate.assert_not_awaited()


async def test_total_deadline_interrupts_generation_and_releases_queue() -> None:
    pool = InMemoryPool()

    async def stall(request):
        await asyncio.Event().wait()

    service = make_dialog(
        pool,
        Settings(bot_token="unused", postgres_password="unused", agent_timeout=0.01),
        MagicMock(generate=AsyncMock(side_effect=stall)),
    )
    with pytest.raises(LLMTimeoutError):
        await service.process_user_message(1, "question")
    assert not await service.is_busy(1)


def test_context_budget_includes_tools_and_output_reserve() -> None:
    budget = ContextBudget(600, output_tokens=100)
    assert not budget.fits([ChatMessage(role=MessageRole.USER, content="я" * 500)], [])
    assert not budget.fits([], [ToolDefinition(name="x", description="x" * 700)])


async def test_cancellation_during_claim_cleans_up_and_releases_queue() -> None:
    pool = InMemoryPool()
    entered = asyncio.Event()

    class SlowClaimStore(MemoryTurnStore):
        async def claim(self, context, now):
            claim = await super().claim(context, now)
            entered.set()
            await asyncio.Event().wait()
            return claim

    store = SlowClaimStore(cast(MemoryHistory, pool))
    llm = MagicMock(generate=AsyncMock())
    service = DialogService(cast(asyncpg.Pool, pool), SETTINGS, llm, store=store)
    task = asyncio.create_task(service.process_user_message(1, "question", event_id="cancelled"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.turns[1, "cancelled"].state == "failed"
    assert not await service.is_busy(1)
    llm.generate.assert_not_awaited()


async def test_compaction_processes_bounded_batches_and_preserves_recent_pairs() -> None:
    pool = InMemoryPool()
    for index in range(100):
        pool.messages.append(
            {
                "id": index + 1,
                "user_id": 1,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"fact {index}",
                "char_count": 10,
                "metadata": {},
            }
        )
    llm = MagicMock(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(content="first summary"),
                LLMResponse(content="combined summary"),
            ]
        )
    )
    service = make_dialog(pool, SETTINGS, llm)
    assert await service.compact_history(1)
    assert len(pool.messages) == 40
    assert await service.compact_history(1)
    assert [message["content"] for message in pool.messages] == [
        f"fact {i}" for i in range(96, 100)
    ]
    second = llm.generate.await_args_list[1].args[0]
    assert "first summary" in second.messages[1].content
    assert pool.summaries[1] == "combined summary"


async def test_oversized_tool_result_does_not_break_json_or_call_model_again() -> None:
    mcp = MockTools(SETTINGS)
    mcp.load_catalog(tool_catalog())
    mcp.call_tool = AsyncMock(return_value={"status": "ok", "large": "x" * 1000})
    llm = MagicMock(
        generate=AsyncMock(
            return_value=LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="weather", name="get_weather", arguments={"city": "Казань"})
                ],
            )
        )
    )
    agent = AgentService(llm, ToolExecutionService(mcp, max_output_chars=100))
    result = await agent.run([], EventContext(user_id=1, event_id="large", request_id="test"))
    assert result.reason == "tool_output_budget"
    llm.generate.assert_awaited_once()


async def test_small_context_rejects_request_before_generation() -> None:
    llm = MagicMock(generate=AsyncMock())
    result = await AgentService(llm).run(
        [ChatMessage(role=MessageRole.USER, content="длинный запрос" * 1000)],
        EventContext(user_id=1, event_id="large", request_id="test"),
        budget=ContextBudget(2048, 256),
    )
    assert result.reason == "context_budget"
    llm.generate.assert_not_awaited()
