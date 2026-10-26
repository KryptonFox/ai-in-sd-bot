import asyncio
import json
import os
import socket
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import uvicorn
from jsonschema import Draft202012Validator, FormatChecker
from mcp import types
from test_integration import database as database_fixture

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import EventContext
from app.core.schemas.tools import ToolFailure
from app.db.connection import create_pool
from app.mcp.client import MCPClient
from app.mcp_server.__main__ import http_app
from app.mcp_server.config import ServerSettings
from app.mcp_server.server import build_server, tool_catalog
from app.services.confirmation_service import ConfirmationService

SECRET = "mcp-client-unit-secret"


@pytest.fixture
def integration_database():
    yield from database_fixture.__wrapped__()


def make_client() -> MCPClient:
    client = MCPClient(
        Settings(bot_token="unused", postgres_password="unused", mcp_shared_secret=SECRET)
    )
    client.load_catalog(tool_catalog())
    return client


def mcp_result(payload: dict, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        isError=is_error,
        structuredContent=payload,
        content=[types.TextContent(type="text", text="payload")],
    )


def valid_schedule() -> dict:
    return {"date": "2026-10-12", "lessons": []}


def valid_reminder() -> dict:
    return {
        "id": 12,
        "text": "Проверить расписание",
        "remind_at": "2026-10-12T11:00:00+03:00",
        "timezone": "Europe/Moscow",
        "created": True,
    }


@pytest.mark.asyncio
async def test_call_tool_validates_input_and_returns_schema_validated_output() -> None:
    client = make_client()
    result = valid_schedule()
    client._command = AsyncMock(return_value=mcp_result(result))

    actual = await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert actual == result
    client._command.assert_awaited_once_with(
        "call_tool", "get_schedule", {"date": "2026-10-12"}, meta=None
    )


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_tool_and_extra_input_fields() -> None:
    client = make_client()
    client._command = AsyncMock(return_value=mcp_result(valid_schedule()))

    with pytest.raises(ToolFailure) as unknown:
        await client.call_tool("delete_all", {})
    with pytest.raises(ToolFailure) as extra:
        await client.call_tool("get_schedule", {"date": "2026-10-12", "path": "/tmp/data"})

    assert unknown.value.code == "unknown_tool"
    assert extra.value.code == "invalid_arguments"
    client._command.assert_not_awaited()


def test_discovered_arguments_enforce_real_iso_date_and_nonblank_text() -> None:
    client = make_client()
    client.validate_arguments("get_schedule", {"date": "2026-10-26"})
    client.validate_arguments(
        "add_reminder", {"text": "Напомнить", "remind_at": "2026-10-27T12:00:00+03:00"}
    )
    with pytest.raises(ToolFailure) as impossible_date:
        client.validate_arguments("get_schedule", {"date": "2026-02-30"})
    with pytest.raises(ToolFailure) as malformed_date:
        client.validate_arguments("get_schedule", {"date": "2026-2-3"})
    with pytest.raises(ToolFailure) as blank_city:
        client.validate_arguments("get_weather", {"city": "   "})
    with pytest.raises(ToolFailure) as blank_text:
        client.validate_arguments(
            "add_reminder", {"text": "  ", "remind_at": "2026-10-27T12:00:00+03:00"}
        )
    assert all(
        error.value.code == "invalid_arguments"
        for error in (impossible_date, malformed_date, blank_city, blank_text)
    )


def test_tool_output_schemas_accept_error_structured_content_union() -> None:
    error_content = {"error": {"code": "invalid_arguments", "message": "Проверьте параметры."}}
    for tool in tool_catalog():
        Draft202012Validator(tool.outputSchema, format_checker=FormatChecker()).validate(
            error_content
        )


@pytest.mark.asyncio
async def test_success_flag_with_error_structured_content_is_rejected() -> None:
    client = make_client()
    client._command = AsyncMock(
        return_value=mcp_result(
            {"error": {"code": "invalid_arguments", "message": "Проверьте параметры."}}
        )
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-26"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_call_tool_rejects_non_finite_weather_numbers() -> None:
    client = make_client()
    client._command = AsyncMock(
        return_value=mcp_result(
            {
                "status": "ok",
                "city": "Paris",
                "country": "France",
                "temperature": float("nan"),
                "weather_code": 1,
                "wind_speed": float("inf"),
                "wind_unit": "km/h",
                "observed_at": "2026-10-26T12:00",
                "timezone": "Europe/Paris",
                "candidates": [],
            }
        )
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_weather", {"city": "Paris"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_call_tool_rejects_output_that_does_not_match_discovered_schema() -> None:
    client = make_client()
    client._command = AsyncMock(return_value=mcp_result({"date": "2026-10-12", "lessons": "none"}))

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"
    assert "payload" not in raised.value.message


@pytest.mark.asyncio
async def test_mutation_requires_context_and_sends_application_meta() -> None:
    client = make_client()
    client._command = AsyncMock(return_value=mcp_result(valid_reminder()))
    arguments = {
        "text": "Проверить расписание",
        "remind_at": "2026-10-12T11:00:00+03:00",
    }

    with pytest.raises(ToolFailure) as missing_context:
        await client.call_tool("add_reminder", arguments)
    assert missing_context.value.code == "confirmation_required"
    client._command.assert_not_awaited()

    actual = await client.call_tool(
        "add_reminder",
        arguments,
        user_id=77,
        confirmation_id="confirm-uuid",
    )

    assert actual == valid_reminder()
    client._command.assert_awaited_once_with(
        "call_tool",
        "add_reminder",
        arguments,
        meta={
            "app_token": SECRET,
            "user_id": 77,
            "confirmation_id": "confirm-uuid",
        },
    )


@pytest.mark.asyncio
async def test_mutation_also_requires_confirmation_id_when_owner_is_present() -> None:
    client = make_client()
    client._command = AsyncMock(return_value=mcp_result(valid_reminder()))

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool(
            "add_reminder",
            {"text": "Проверить", "remind_at": "2026-10-12T11:00:00+03:00"},
            user_id=77,
        )

    assert raised.value.code == "confirmation_required"
    client._command.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_error_is_converted_to_safe_tool_failure() -> None:
    client = make_client()
    client._command = AsyncMock(
        return_value=mcp_result(
            {"error": {"code": "provider_down", "message": "sensitive provider detail"}},
            is_error=True,
        )
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "provider_down"
    assert "sensitive provider detail" not in raised.value.message


@pytest.mark.asyncio
async def test_client_can_close_and_start_again(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    catalog = tool_catalog()

    async def fake_worker() -> None:
        client.load_catalog(catalog)
        client._ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_worker", fake_worker)

    assert await client.start() is True
    await client.aclose()
    assert client.available is False
    assert client._task is None

    assert await client.start() is True
    await client.aclose()
    assert client.available is False


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
)
async def test_stdio_client_discovers_reads_and_confirms_reminder_with_database(
    integration_database, tmp_path
) -> None:

    base_settings, *_ = integration_database
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    schedule = {
        today.isoformat(): [
            {
                "start": "09:00",
                "end": "10:30",
                "title": "Интеграционная лекция",
                "location": "Аудитория 9",
            }
        ]
    }
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(schedule, ensure_ascii=False), encoding="utf-8")
    mcp_settings = replace(
        base_settings,
        mcp_transport="stdio",
        mcp_shared_secret="stdio-integration-secret-0123456789",
        schedule_path=str(schedule_path),
        schedule_timezone="Europe/Moscow",
        mcp_timeout=15,
    )
    pool = await create_pool(base_settings)
    client = MCPClient(mcp_settings)
    now = datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0)
    clock = FrozenClock(now)
    confirmations = ConfirmationService(pool, client, clock=clock, ttl=120)
    user_id = 970_001
    context = EventContext(
        user_id=user_id,
        event_id="mcp-client-stdio-reminder",
        request_id="mcp-client-stdio-request",
    )
    arguments = {
        "text": "Проверить интеграционный сценарий",
        "remind_at": (now + timedelta(days=2)).isoformat(),
    }

    try:
        assert await client.start() is True
        assert set(client.tools) == {"get_weather", "get_schedule", "add_reminder"}
        schedule_result = await client.call_tool("get_schedule", {"date": today.isoformat()})
        assert schedule_result["lessons"][0]["title"] == "Интеграционная лекция"
        week_result = await client.read_week()
        assert week_result["start_date"] == monday.isoformat()
        day = next(item for item in week_result["days"] if item["date"] == today.isoformat())
        assert day["lessons"][0]["title"] == "Интеграционная лекция"

        prepared = await confirmations.prepare(
            context,
            arguments,
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert prepared.decision == "prepare"
        assert prepared.confirmation_id
        confirmed = await confirmations.confirm(user_id, prepared.confirmation_id)
        assert confirmed.decision == "tool"
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", user_id) == 1

        with pytest.raises(ToolFailure) as wrong_owner:
            await client.call_tool(
                "add_reminder",
                arguments,
                user_id=user_id + 1,
                confirmation_id=prepared.confirmation_id,
            )
        assert wrong_owner.value.code == "unauthorized"
        assert await pool.fetchval("SELECT count(*) FROM reminders WHERE user_id=$1", user_id) == 1
    finally:
        await client.aclose()
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
)
async def test_streamable_http_client_reads_and_rejects_bad_auth(tmp_path) -> None:

    class ReadOnlyPool:
        async def fetchval(self, query: str, **kwargs) -> int:
            return 1

    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps({"2026-10-26": []}), encoding="utf-8")
    secret = "streamable-http-integration-secret-012345"
    server_settings = ServerSettings(
        postgres_password="unused",
        mcp_shared_secret=secret,
        schedule_path=Path(schedule_path),
        schedule_timezone="Europe/Moscow",
    )
    weather_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    )
    pool = ReadOnlyPool()
    app = http_app(build_server(pool, server_settings, weather_http), secret, pool)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    )
    server_task = asyncio.create_task(server.serve())
    client = None
    unauthorized_client = None
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if server_task.done():
                await server_task
            if time.monotonic() >= deadline:
                raise TimeoutError("test HTTP server did not start")
            await asyncio.sleep(0.02)

        client = MCPClient(
            Settings(
                bot_token="unused",
                postgres_password="unused",
                mcp_transport="streamable-http",
                mcp_shared_secret=secret,
                mcp_server_url=f"http://127.0.0.1:{port}/mcp/",
                mcp_timeout=5,
            )
        )
        assert await client.start() is True
        assert "get_schedule" in client.tools
        schedule_result = await client.call_tool("get_schedule", {"date": "2026-10-26"})
        assert schedule_result == {"date": "2026-10-26", "lessons": []}

        unauthorized_client = MCPClient(
            Settings(
                bot_token="unused",
                postgres_password="unused",
                mcp_transport="streamable-http",
                mcp_shared_secret="incorrect-streamable-http-secret-0123456789",
                mcp_server_url=f"http://127.0.0.1:{port}/mcp/",
                mcp_timeout=5,
            )
        )
        unauthorized_client.load_catalog(tool_catalog())
        unauthorized_client.available = False
        with pytest.raises(ToolFailure) as unauthorized:
            await unauthorized_client.call_tool("get_schedule", {"date": "2026-10-26"})
        assert unauthorized.value.code == "mcp_unavailable"
    finally:
        if client is not None:
            await client.aclose()
        if unauthorized_client is not None:
            await unauthorized_client.aclose()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        await weather_http.aclose()
