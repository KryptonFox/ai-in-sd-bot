import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from mcp import types

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, EventContext
from app.core.schemas.llm import FinishReason, LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ScheduleWeekArguments, ToolFailure
from app.mcp.client import MCPClient
from app.mcp.connection import create_student_tools_client
from app.services.agent_service import AgentService
from app.services.tool_execution import READ_RESOURCE_FUNCTION, ToolExecutionService
from mcp_servers.student_tools.schedule import ScheduleService
from mcp_servers.student_tools.weather import WeatherService
from tests.fake_mcp import mock_sdk_client
from tests.mcp_catalog import resource_catalog, tool_catalog

CLOCK = FrozenClock(datetime.fromisoformat("2026-10-27T23:30:00+03:00"))


async def test_discovered_week_resource_reaches_agent_and_counts_as_one_call() -> None:
    sdk, sdk_mock = mock_sdk_client()
    client = MCPClient(sdk)
    client.load_catalog(tool_catalog())
    client.load_resources(resource_catalog())
    week = {
        "timezone": "Europe/Moscow",
        "start_date": "2026-10-26",
        "end_date": "2026-11-01",
        "days": [],
    }
    command = AsyncMock(
        return_value=[
            types.TextResourceContents(
                uri="schedule://current-week",
                mime_type="application/json",
                text=json.dumps(week),
            )
        ]
    )
    sdk_mock.read_resource = command
    client.available = True
    llm = Mock()
    llm.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[
                    ToolCall(
                        id="week",
                        name=READ_RESOURCE_FUNCTION,
                        arguments={"uri": "schedule://current-week"},
                    )
                ],
            ),
            LLMResponse(content="Расписание недели получено."),
        ]
    )
    store = Mock(reserve=AsyncMock(return_value=True))
    result = await AgentService(llm, ToolExecutionService(client, store=store)).run(
        [ChatMessage(role=MessageRole.USER, content="Когда я свободен на этой неделе?")],
        EventContext(user_id=1, event_id="week", request_id="week"),
    )
    assert result.decision == AgentDecision.RESPOND
    assert result.tool_name is None
    assert result.resource_uris == ["schedule://current-week"]
    command.assert_awaited_once_with("schedule://current-week")
    store.reserve.assert_awaited_once()
    resource_message = llm.generate.await_args_list[1].args[0].messages[-1]
    assert resource_message.role == MessageRole.TOOL
    assert resource_message.tool_call_id == "week"
    resource_payload = json.loads(resource_message.content)
    assert resource_payload["source"] == "mcp_resource"
    assert resource_payload["uri"] == "schedule://current-week"
    assert json.loads(resource_payload["content"])["start_date"] == "2026-10-26"


async def test_resource_adapter_rejects_undiscovered_and_arbitrary_uris() -> None:
    client = create_student_tools_client(Settings(bot_token="unused", postgres_password="unused"))
    client.load_catalog(tool_catalog())
    assert "read_resource" not in client.tools
    client.load_resources(resource_catalog())
    resource_tool = next(
        tool
        for tool in AgentService(Mock(), ToolExecutionService(client)).definitions
        if tool.name == READ_RESOURCE_FUNCTION
    )
    assert resource_tool.parameters["properties"]["uri"]["enum"] == ["schedule://current-week"]
    Draft202012Validator(resource_tool.parameters).validate({"uri": "schedule://current-week"})

    for uri in ("file:///etc/passwd", "https://example.org", "schedule://next-week"):
        with pytest.raises(ToolFailure):
            await client.read_resource(uri)

    with pytest.raises(SchemaValidationError):
        Draft202012Validator(resource_tool.parameters).validate(
            {"uri": "schedule://current-week", "path": "/etc/passwd"}
        )

    client.load_resources([])
    assert READ_RESOURCE_FUNCTION not in {
        tool.name for tool in AgentService(Mock(), ToolExecutionService(client)).definitions
    }


async def test_week_crosses_month_boundary_and_distinguishes_unknown_days(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"2026-11-01": []}), encoding="utf-8")
    service = ScheduleService(path, "Europe/Moscow", CLOCK)
    week = await service.week_containing("2026-11-01")
    assert (week.start_date, week.end_date) == ("2026-10-26", "2026-11-01")
    assert len(week.days) == 7
    assert week.days[0].weekday == "понедельник" and not week.days[0].data_available
    assert week.days[0].is_free_day is None
    assert week.days[-1].weekday == "воскресенье" and week.days[-1].data_available
    assert week.days[-1].is_free_day is True
    assert week.free_days == ["2026-11-01"]
    assert all(day.lessons == [] for day in week.days)


async def test_relative_week_uses_server_calendar_and_timezone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule.json"
    path.write_text("{}", encoding="utf-8")
    clock = FrozenClock(datetime.fromisoformat("2026-11-01T23:30:00+00:00"))
    service = ScheduleService(path, "Europe/Moscow", clock)
    week = await service.relative_week(1)
    assert (week.start_date, week.end_date) == ("2026-11-09", "2026-11-15")
    client = create_student_tools_client(Settings(bot_token="unused", postgres_password="unused"))
    client.load_catalog(tool_catalog())
    client.validate_arguments("get_schedule_week", {"week_offset": 1})

    with pytest.raises(ValueError):
        ScheduleWeekArguments(date="2026-11-03", week_offset=1)


async def test_weekly_schedule_uses_one_snapshot_for_all_days(
    tmp_path: Path,
) -> None:
    service = ScheduleService(
        tmp_path / "schedule.json",
        "Europe/Moscow",
        CLOCK,
    )
    read_data = Mock(side_effect=[{"2026-10-28": []}, {}])
    service._read_data = read_data

    week = await service.week_containing("2026-10-27")

    assert week.days[2].date == "2026-10-28"
    assert week.days[2].data_available and week.days[2].is_free_day
    read_data.assert_called_once()


def future_weather() -> dict[str, object]:
    return {
        "timezone": "Europe/Moscow",
        "current": {
            "temperature_2m": 4,
            "weather_code": 0,
            "wind_speed_10m": 3,
            "time": "2026-10-27T23:30",
        },
        "current_units": {"wind_speed_10m": "km/h"},
        "daily": {
            "time": ["2026-10-27", "2026-10-28"],
            "precipitation_probability_max": [0, 90],
            "precipitation_sum": [0, 5],
            "temperature_2m_min": [1, -2],
            "temperature_2m_max": [10, 6],
            "weather_code": [0, 61],
            "wind_speed_10m_max": [5, 20],
        },
        "daily_units": {
            "precipitation_probability_max": "%",
            "precipitation_sum": "mm",
            "temperature_2m_min": "°C",
            "temperature_2m_max": "°C",
            "weather_code": "wmo code",
            "wind_speed_10m_max": "km/h",
        },
    }


def location() -> dict[str, object]:
    return {"name": "Казань", "country": "Россия", "latitude": 55.79, "longitude": 49.12}


async def test_tomorrow_uses_requested_forecast_instead_of_current_conditions() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [location()]})

        assert request.url.params["forecast_days"] == "16"
        return httpx.Response(200, json=future_weather())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань", "2026-10-28")

    assert result.temperature == 4
    assert result.today is not None and result.today.precipitation_probability == 0
    assert result.forecast is not None and result.forecast.precipitation_probability == 90
    assert result.forecast.temperature_min == -2 and result.forecast.temperature_max == 6
    assert result.forecast.wind_speed_max == 20
    assert result.condition == "ясно" and result.forecast.condition == "слабый дождь"
    assert result.forecast.date == result.requested_date


@pytest.mark.parametrize(
    "dates",
    [
        ["2026-10-27", "2026-10-27"],
        ["2026-10-27", "2026-10-29"],
        ["2026-10-28", "2026-10-27"],
    ],
)
async def test_forecast_rejects_duplicate_skipped_and_reversed_days(
    dates: list[str],
) -> None:
    data = future_weather()
    daily = data["daily"]
    assert isinstance(daily, dict)
    daily["time"] = dates

    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [location()]})

        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ToolFailure) as failure:
            await WeatherService(client, clock=CLOCK).get_weather("Казань", "2026-10-28")

    assert failure.value.code == "weather_unavailable"


@pytest.mark.parametrize("missing_day", [0, 1])
async def test_missing_rain_values_do_not_discard_another_days_forecast(
    missing_day: int,
) -> None:
    data = future_weather()
    daily = data["daily"]
    assert isinstance(daily, dict)
    amounts = daily["precipitation_sum"]
    assert isinstance(amounts, list)
    amounts[missing_day] = None

    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [location()]})

        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань", "2026-10-28")

    if missing_day == 0:
        assert result.today is None
        assert result.forecast is not None and result.forecast.precipitation == 5
    else:
        assert result.forecast is None
        assert result.today is not None and result.today.precipitation == 0


async def test_weather_uses_one_calendar_day_when_clock_crosses_midnight() -> None:
    clock = Mock()
    clock.now.side_effect = [
        datetime.fromisoformat("2026-10-27T23:59:59+03:00"),
        datetime.fromisoformat("2026-10-28T00:00:01+03:00"),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [location()]})

        return httpx.Response(200, json=future_weather())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=clock).get_weather("Казань")

    assert result.today is not None and result.forecast is not None
    assert result.requested_date is not None
    assert result.requested_date.isoformat() == "2026-10-27"
    assert result.today == result.forecast


@pytest.mark.parametrize("day", ["2026-10-26", "2026-11-12"])
async def test_forecast_rejects_dates_outside_supported_horizon(
    day: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [location()]} if "geocoding" in request.url.host else future_weather(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ToolFailure) as raised:
            await WeatherService(client, clock=CLOCK).get_weather("Казань", day)

    assert raised.value.code == "forecast_date_unavailable"


async def test_missing_requested_forecast_does_not_substitute_today() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [location()]} if "geocoding" in request.url.host else future_weather(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather("Казань", "2026-10-29")

    assert result.forecast is None and result.today is not None
    assert result.requested_date is not None and result.requested_date.isoformat() == "2026-10-29"


@pytest.mark.parametrize("query", ["Казань", "Kazan"])
async def test_geocoding_network_failure_falls_back_without_choosing_airport_or_village(
    query: str,
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "geocoding" in request.url.host:
            raise httpx.ConnectError("private transport detail", request=request)

        if "photon" in request.url.host:
            assert request.url.params["q"] == query
            assert request.headers["User-Agent"] == "itmo-ai-in-sd-bot/0.1"
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "name": query if value == "village" else "Казань",
                                "country": "Россия",
                                "type": kind,
                                "osm_value": value,
                            },
                            "geometry": {"coordinates": coordinates},
                        }
                        for kind, value, coordinates in (
                            ("house", "aerodrome", [49.28, 55.60]),
                            ("city", "village", [51, 58]),
                            ("city", "city", [49.12, 55.79]),
                        )
                    ]
                },
            )

        assert request.url.params["latitude"] == "55.79"

        return httpx.Response(200, json=future_weather())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await WeatherService(client, clock=CLOCK).get_weather(query, "2026-10-28")

    assert result.status == "ok" and result.city == "Казань"
    assert len(calls) == 4
