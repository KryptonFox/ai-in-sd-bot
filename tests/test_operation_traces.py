import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import User

from app.core.schemas.tools import ToolFailure
from app.core.trace import (
    OperationTrace,
    TraceStatus,
    TraceStep,
    bind_trace,
    current_trace,
    trace_step,
)
from app.db.connection import create_pool
from app.db.repositories.trace_repo import find_trace, purge_traces, save_trace
from app.handlers import trace as trace_handler
from tests.conftest import DatabaseFixture

NOW = datetime(2026, 11, 26, 10, tzinfo=UTC)


def test_trace_hierarchy_and_safe_error_code() -> None:
    trace = OperationTrace(101, "safe-request", NOW, route="schedule")

    with bind_trace(trace), trace_step("specialist", "schedule") as outer:
        with (
            pytest.raises(ToolFailure),
            trace_step("mcp", "call_tool", tool_name="get_schedule"),
        ):
            raise ToolFailure("mcp_timeout", "Internal message must stay out of the trace")

    assert current_trace() is None
    assert outer is not None
    assert len(trace.steps) == 2
    assert trace.steps[0].status is TraceStatus.OK
    child = trace.steps[1]
    assert child.parent_id == outer.id
    assert child.status is TraceStatus.ERROR
    assert child.error_code == "mcp_timeout"
    assert child.duration_ms >= 0
    assert "Internal message" not in repr(trace.steps)


async def test_trace_handler_rejects_invalid_selector_without_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(trace_handler, "find_trace", lookup)
    message = MagicMock()
    message.from_user = User(id=101, is_bot=False, first_name="Owner")
    message.text = "/trace not-a-uuid"
    message.answer = AsyncMock()

    await trace_handler.handle_trace(message, MagicMock())

    lookup.assert_not_awaited()
    message.answer.assert_awaited_once_with("Трасса не найдена.")


async def test_trace_handler_shows_owner_steps_without_user_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = OperationTrace(101, "request-1", NOW, route="schedule")
    trace.steps = [
        TraceStep("specialist", "schedule", NOW, None, duration_ms=12.4),
        TraceStep(
            "mcp",
            "call_tool",
            NOW,
            None,
            duration_ms=3.2,
            tool_name="get_schedule",
        ),
    ]
    lookup = AsyncMock(return_value=trace)
    monkeypatch.setattr(trace_handler, "find_trace", lookup)
    message = MagicMock()
    message.from_user = User(id=101, is_bot=False, first_name="Owner")
    message.text = "/trace last"
    message.answer = AsyncMock()

    await trace_handler.handle_trace(message, MagicMock())

    lookup.assert_awaited_once()
    assert lookup.await_args is not None
    assert lookup.await_args.args[1] == 101
    answer_call = message.answer.await_args
    assert answer_call is not None
    rendered = answer_call.args[0]
    assert f"Трасса {trace.id}" in rendered
    assert "Маршрут schedule" in rendered
    assert "specialist schedule" in rendered
    assert "get_schedule" in rendered


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="Docker PostgreSQL")
async def test_trace_owner_retention_and_retry_append(database: DatabaseFixture) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    trace_id = uuid4()
    try:
        await pool.execute("INSERT INTO user_settings (user_id) VALUES (101), (202)")
        first_attempt = OperationTrace(101, "notification-request", NOW, id=trace_id)
        second_attempt = OperationTrace(101, "notification-request", NOW, id=trace_id)
        another_owner = OperationTrace(202, "other-request", NOW, id=trace_id)
        for trace, action in (
            (first_attempt, "retry"),
            (second_attempt, "sent"),
            (another_owner, "other"),
        ):
            with bind_trace(trace), trace_step("telegram", action):
                pass
            await save_trace(pool, trace)
        await save_trace(pool, second_attempt)

        stored = await find_trace(pool, 101, NOW, trace_id)
        assert stored is not None
        assert [step.action for step in stored.steps] == ["retry", "sent"]
        assert await find_trace(pool, 202, NOW, trace_id) is None
        expired = NOW + timedelta(days=31)
        assert await find_trace(pool, 101, expired, trace_id) is None
        await purge_traces(pool, expired)
        assert await pool.fetchval("SELECT count(*) FROM operation_traces") == 0
        assert await pool.fetchval("SELECT count(*) FROM operation_trace_steps") == 0
    finally:
        await pool.close()
