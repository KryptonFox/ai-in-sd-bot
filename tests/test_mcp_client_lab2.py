import asyncio
import json
import os
import socket
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ResourceError
from mcp import types

from app.config import MCPTransport, Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, EventContext
from app.core.schemas.tools import ScheduleResult, ToolFailure, WeekSchedule
from app.db.connection import create_pool
from app.mcp.client import MCPClient
from app.mcp.connection import create_student_tools_client
from app.services.confirmation_service import ConfirmationService
from mcp_servers.student_tools.__main__ import http_app
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server
from tests.fake_mcp import mock_sdk_client
from tests.mcp_catalog import resource_catalog, tool_catalog

SECRET = "mcp-client-unit-secret"


def make_client() -> MCPClient:
    sdk, _ = mock_sdk_client()
    client = MCPClient(sdk, secret=SECRET)
    client.load_catalog(tool_catalog())
    client.available = True
    return client


def mcp_result(payload: dict, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        is_error=is_error,
        structured_content=payload,
        content=[types.TextContent(type="text", text="payload")],
        meta=None,
    )


def mock_client_call(client: MCPClient, result: CallToolResult) -> AsyncMock:
    call_tool = AsyncMock(return_value=result)
    cast(AsyncMock, client._client).call_tool = call_tool
    client.available = True
    return call_tool


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
    command = mock_client_call(client, mcp_result(result))

    actual = await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert actual == result
    command.assert_awaited_once_with(
        "get_schedule",
        {"date": "2026-10-12"},
        meta=None,
        raise_on_error=False,
    )


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_tool_and_extra_input_fields() -> None:
    client = make_client()
    command = mock_client_call(client, mcp_result(valid_schedule()))

    with pytest.raises(ToolFailure) as unknown:
        await client.call_tool("delete_all", {})
    with pytest.raises(ToolFailure) as extra:
        await client.call_tool("get_schedule", {"date": "2026-10-12", "path": "/tmp/data"})

    assert unknown.value.code == "unknown_tool"
    assert extra.value.code == "invalid_arguments"
    command.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_success_flag_with_error_structured_content_is_rejected() -> None:
    client = make_client()
    mock_client_call(
        client,
        mcp_result({"error": {"code": "invalid_arguments", "message": "Проверьте параметры."}}),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-26"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_tool_error_parses_safe_json_text_without_echoing_message() -> None:
    client = make_client()
    mock_client_call(
        client,
        CallToolResult(
            is_error=True,
            structured_content=None,
            content=[
                types.TextContent(
                    type="text",
                    text='{"error":{"code":"provider_down","message":"private detail"}}',
                )
            ],
            meta=None,
        ),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-26"})

    assert raised.value.code == "provider_down"
    assert "private detail" not in raised.value.message


@pytest.mark.asyncio
async def test_sdk_timeout_returns_safe_failure_without_closing_shared_session() -> None:
    client = make_client()
    command = AsyncMock(side_effect=TimeoutError("private transport detail"))
    cast(AsyncMock, client._client).call_tool = command
    client.available = True

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-26"})

    assert raised.value.code == "mcp_timeout"
    assert "private transport detail" not in raised.value.message
    assert client.available is True
    command.assert_awaited_once()
    cast(AsyncMock, client._client).close.assert_not_awaited()


@pytest.mark.asyncio
async def test_fastmcp_request_timeout_code_maps_to_timeout_failure() -> None:
    server = FastMCP("slow-tools")

    @server.tool
    async def get_schedule(date: str) -> dict[str, str]:
        await asyncio.sleep(0.2)
        return {"date": date}

    sdk_client = Client(server, timeout=0.05)
    await sdk_client.__aenter__()
    client = MCPClient(sdk_client, secret=SECRET)
    client.load_catalog(await sdk_client.list_tools())
    client.available = True

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool(
            "get_schedule",
            {"date": "2026-10-26"},
            user_id=77,
            confirmation_id="confirm-uuid",
        )

    assert raised.value.code == "mcp_timeout"
    assert client.available is True
    await client.aclose()


@pytest.mark.asyncio
async def test_resource_only_discovery_is_available_and_reads_single_matching_text() -> None:
    client = make_client()
    client.load_catalog([])
    client.load_resources(resource_catalog())
    content = types.TextResourceContents(
        uri="schedule://current-week",
        text='{"start_date":"2026-10-26"}',
        mime_type="application/json",
    )
    read_resource = AsyncMock(return_value=[content])
    cast(AsyncMock, client._client).read_resource = read_resource
    client.available = True

    result = await client.read_resource("schedule://current-week")

    assert client.available is True
    assert result.uri == "schedule://current-week"
    assert result.text == content.text
    assert result.mime_type == "application/json"
    read_resource.assert_awaited_once_with("schedule://current-week")


@pytest.mark.asyncio
async def test_resource_error_is_safe_and_does_not_disconnect_client() -> None:
    client = make_client()
    client.load_catalog([])
    client.load_resources(resource_catalog())
    read_resource = AsyncMock(side_effect=ResourceError("private resource detail"))
    cast(AsyncMock, client._client).read_resource = read_resource
    client.available = True

    with pytest.raises(ToolFailure) as raised:
        await client.read_resource("schedule://current-week")

    assert raised.value.code == "resource_unavailable"
    assert "private resource detail" not in raised.value.message
    assert client.available is True
    read_resource.assert_awaited_once_with("schedule://current-week")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
)
async def test_stdio_client_discovers_reads_and_confirms_reminder_with_database(
    database, tmp_path
) -> None:
    base_settings, *_ = database
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                today.isoformat(): [
                    {
                        "start": "09:00",
                        "end": "10:30",
                        "title": "Интеграционная лекция",
                        "location": "Аудитория 9",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    secret = "stdio-integration-secret-0123456789"
    mcp_settings = replace(
        base_settings,
        mcp_transport=MCPTransport.STDIO,
        mcp_shared_secret=secret,
        schedule_path=str(schedule_path),
        schedule_timezone="Europe/Moscow",
        mcp_timeout=15,
    )
    pool = await create_pool(base_settings)
    client = create_student_tools_client(mcp_settings)
    now = datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0)
    confirmations = ConfirmationService(pool, client, clock=FrozenClock(now), ttl=120)
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
        assert set(client.tools) == {
            "get_weather",
            "get_schedule",
            "get_schedule_week",
            "add_reminder",
        }
        assert "read_resource" not in client.tools
        schedule_result = await client.call_tool("get_schedule", {"date": today.isoformat()})
        assert ScheduleResult.model_validate(schedule_result).lessons[0].title == (
            "Интеграционная лекция"
        )
        week_content = await client.read_resource("schedule://current-week")
        week = WeekSchedule.model_validate_json(week_content.text)
        assert week.start_date == monday.isoformat()
        day = next(item for item in week.days if item.date == today.isoformat())
        assert day.lessons[0].title == "Интеграционная лекция"

        prepared = await confirmations.prepare(
            context,
            arguments,
            "Europe/Moscow",
            position=0,
            calls_remaining=2,
        )
        assert prepared.decision == AgentDecision.PREPARE
        assert prepared.confirmation_id
        confirmed = await confirmations.confirm(user_id, prepared.confirmation_id)
        assert confirmed.decision == AgentDecision.TOOL
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
        await client.aclose()
        sdk_client = client._client
        assert sdk_client.is_connected() is False
        assert await client.start() is True
    finally:
        await client.aclose()
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
)
async def test_streamable_http_client_auth_failure_and_reconnect(tmp_path, database) -> None:
    base_settings, *_ = database
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps({"2026-11-02": []}), encoding="utf-8")
    secret = "streamable-http-integration-secret-012345"
    server_settings = ServerSettings(
        postgres_password=base_settings.postgres_password,
        postgres_host=base_settings.postgres_host,
        postgres_port=base_settings.postgres_port,
        postgres_db=base_settings.postgres_db,
        postgres_user=base_settings.postgres_user,
        mcp_shared_secret=secret,
        schedule_path=Path(schedule_path),
        schedule_timezone="Europe/Moscow",
    )
    weather_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    )
    pool = await create_pool(base_settings)
    app = http_app(build_server(pool, server_settings, weather_http), secret, pool)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    client: MCPClient | None = None
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if server_task.done():
                await server_task
            if time.monotonic() >= deadline:
                raise TimeoutError("test HTTP server did not start")
            await asyncio.sleep(0.02)

        bad_client = create_student_tools_client(
            Settings(
                bot_token="unused",
                postgres_password="unused",
                mcp_transport=MCPTransport.STREAMABLE_HTTP,
                mcp_shared_secret="incorrect-streamable-http-secret-0123456789",
                mcp_server_url=f"http://127.0.0.1:{port}/mcp/",
                mcp_timeout=5,
            )
        )
        assert await bad_client.start() is False
        assert bad_client.available is False
        assert await pool.fetchval("SELECT count(*) FROM reminders") == 0
        await bad_client.aclose()

        client = create_student_tools_client(
            Settings(
                bot_token="unused",
                postgres_password="unused",
                mcp_transport=MCPTransport.STREAMABLE_HTTP,
                mcp_shared_secret=secret,
                mcp_server_url=f"http://127.0.0.1:{port}/mcp/",
                mcp_timeout=5,
            )
        )
        assert await client.start() is True
        assert "get_schedule" in client.tools
        schedule_result = await client.call_tool("get_schedule", {"date": "2026-11-02"})
        assert schedule_result["date"] == "2026-11-02"
        resource = await client.read_resource("schedule://current-week")
        assert json.loads(resource.text)["start_date"]
    finally:
        if client is not None:
            await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        await weather_http.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_call_tool_rejects_non_finite_weather_numbers() -> None:
    client = make_client()
    mock_client_call(
        client,
        mcp_result(
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
        ),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_weather", {"city": "Paris"})

    assert raised.value.code == "invalid_output"


@pytest.mark.asyncio
async def test_call_tool_rejects_output_that_does_not_match_discovered_schema() -> None:
    client = make_client()
    mock_client_call(
        client,
        mcp_result({"date": "2026-10-12", "lessons": "none"}),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "invalid_output"
    assert "payload" not in raised.value.message


@pytest.mark.asyncio
async def test_mutation_requires_context_and_sends_application_meta() -> None:
    client = make_client()
    command = mock_client_call(client, mcp_result(valid_reminder()))
    arguments = {
        "text": "Проверить расписание",
        "remind_at": "2026-10-12T11:00:00+03:00",
    }

    with pytest.raises(ToolFailure) as missing_context:
        await client.call_tool("add_reminder", arguments)
    assert missing_context.value.code == "confirmation_required"
    command.assert_not_awaited()

    actual = await client.call_tool(
        "add_reminder",
        arguments,
        user_id=77,
        confirmation_id="confirm-uuid",
    )

    assert actual == valid_reminder()
    command.assert_awaited_once_with(
        "add_reminder",
        arguments,
        meta={
            "app_token": SECRET,
            "user_id": 77,
            "confirmation_id": "confirm-uuid",
        },
        raise_on_error=False,
    )


@pytest.mark.asyncio
async def test_mutation_also_requires_confirmation_id_when_owner_is_present() -> None:
    client = make_client()
    command = mock_client_call(client, mcp_result(valid_reminder()))

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool(
            "add_reminder",
            {"text": "Проверить", "remind_at": "2026-10-12T11:00:00+03:00"},
            user_id=77,
        )

    assert raised.value.code == "confirmation_required"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovered_tool_without_read_only_hint_is_treated_as_mutation() -> None:
    client = make_client()
    client.load_catalog(
        [
            types.Tool(
                name="custom_write",
                input_schema={"type": "object", "additionalProperties": False},
                output_schema={"type": "object"},
            )
        ]
    )
    command = mock_client_call(client, mcp_result({}))

    assert client.is_read_only("custom_write") is False
    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("custom_write", {})

    assert raised.value.code == "confirmation_required"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_error_is_converted_to_safe_tool_failure() -> None:
    client = make_client()
    mock_client_call(
        client,
        mcp_result(
            {"error": {"code": "provider_down", "message": "sensitive provider detail"}},
            is_error=True,
        ),
    )

    with pytest.raises(ToolFailure) as raised:
        await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert raised.value.code == "provider_down"
    assert "sensitive provider detail" not in raised.value.message


@pytest.mark.asyncio
async def test_client_can_close_and_start_again() -> None:
    sdk_client, sdk = mock_sdk_client()
    sdk.list_tools.return_value = tool_catalog()
    sdk.list_resources.return_value = []
    client = MCPClient(sdk_client, secret=SECRET)

    assert await client.start() is True
    await client.aclose()
    assert client.available is False

    assert await client.start() is True
    await client.aclose()
    assert client.available is False
    assert sdk.__aenter__.await_count == 2
    assert sdk.close.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_start_and_resource_reads_share_one_sdk_session() -> None:
    server = FastMCP("concurrent-resource")

    @server.resource("test://shared")
    def shared_resource() -> str:
        return "shared"

    client = MCPClient(Client(server))

    assert await asyncio.gather(client.start(), client.start()) == [True, True]
    results = await asyncio.gather(
        client.read_resource("test://shared"),
        client.read_resource("test://shared"),
    )

    assert [result.text for result in results] == ["shared", "shared"]
    await client.aclose()


@pytest.mark.asyncio
async def test_timed_out_request_does_not_close_parallel_request() -> None:
    server = FastMCP("parallel-timeouts")
    slow_started = asyncio.Event()

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=True))
    async def get_schedule(delay: float, date: str) -> dict[str, str]:
        if date == "slow":
            slow_started.set()
        await asyncio.sleep(delay)
        return {"date": date}

    sdk = Client(server, timeout=0.1)
    client = MCPClient(sdk)
    assert await client.start() is True

    slow = asyncio.create_task(client.call_tool("get_schedule", {"delay": 0.12, "date": "slow"}))
    await slow_started.wait()
    await asyncio.sleep(0.05)
    faster = asyncio.create_task(client.call_tool("get_schedule", {"delay": 0.08, "date": "fast"}))
    slow_result, fast_result = await asyncio.gather(slow, faster, return_exceptions=True)

    assert isinstance(slow_result, ToolFailure)
    assert slow_result.code == "mcp_timeout"
    assert fast_result == {"date": "fast"}
    assert client.available is True
    await client.aclose()


@pytest.mark.asyncio
async def test_cancelling_one_tool_request_keeps_shared_sdk_session_connected() -> None:
    sdk, sdk_mock = mock_sdk_client()
    client = MCPClient(sdk, secret=SECRET)
    client.load_catalog(tool_catalog())
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    second_started = asyncio.Event()
    finish_second = asyncio.Event()

    async def call_tool(name: str, arguments: dict[str, str], **_kwargs: object):
        if arguments["date"] == "2026-10-12":
            first_started.set()
            await finish_first.wait()
        if arguments["date"] == "2026-10-13":
            second_started.set()
            await finish_second.wait()
        return mcp_result(valid_schedule())

    sdk_mock.call_tool.side_effect = call_tool
    first = asyncio.create_task(client.call_tool("get_schedule", {"date": "2026-10-12"}))
    await first_started.wait()
    second = asyncio.create_task(client.call_tool("get_schedule", {"date": "2026-10-13"}))
    await second_started.wait()
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    assert client.available is True
    sdk_mock.close.assert_not_awaited()
    finish_second.set()
    assert await second == valid_schedule()
    await client.aclose()


@pytest.mark.asyncio
async def test_request_error_recovers_without_replaying_mutation() -> None:
    sdk = AsyncMock(spec=Client)
    sdk.__aenter__.return_value = sdk
    sdk.__aexit__.return_value = False
    sdk.list_tools.return_value = tool_catalog()
    sdk.list_resources.return_value = []
    sdk.call_tool.side_effect = [
        RuntimeError("private transport detail"),
        mcp_result(valid_schedule()),
    ]
    client = MCPClient(cast(Client, sdk), secret=SECRET)
    client.load_catalog(tool_catalog())

    with pytest.raises(ToolFailure) as failed_mutation:
        await client.call_tool(
            "add_reminder",
            {
                "text": "Проверить расписание",
                "remind_at": "2026-10-14T11:00:00+03:00",
            },
            user_id=42,
            confirmation_id="confirmed-reminder",
        )

    assert failed_mutation.value.code == "mcp_unavailable"
    assert client.available is False
    recovered = await client.call_tool("get_schedule", {"date": "2026-10-12"})

    assert recovered == valid_schedule()
    assert sdk.call_tool.await_count == 2
    assert sdk.call_tool.await_args_list[0].args[0] == "add_reminder"
    assert sdk.call_tool.await_args_list[1].args[0] == "get_schedule"
    assert sdk.close.await_count == 0
    assert sdk.__aenter__.await_count == 2
