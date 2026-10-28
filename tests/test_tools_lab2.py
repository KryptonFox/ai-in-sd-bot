import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.core.clock import FrozenClock, SystemClock
from app.core.schemas.tools import (
    ReminderArguments,
    ScheduleArguments,
    ToolFailure,
    WeatherArguments,
    WeatherResult,
)
from app.core.timezones import normalize_reminder
from app.mcp_server.schedule import ScheduleService
from app.mcp_server.weather import WeatherService


def test_tool_argument_dtos_strip_and_reject_invalid_values() -> None:
    assert WeatherArguments().city == "Санкт-Петербург"
    assert WeatherArguments().date is None
    assert ScheduleArguments().date is None
    assert ScheduleArguments(date=None).date is None
    assert WeatherArguments(city="  Казань  ").city == "Казань"
    assert (
        ReminderArguments(text="  купить молоко ", remind_at="2026-10-27T12:00:00+03:00").text
        == "купить молоко"
    )
    assert ScheduleArguments(date="2026-10-26").date == "2026-10-26"
    with pytest.raises(ValidationError):
        WeatherArguments(city="  ")
    with pytest.raises(ValidationError):
        WeatherArguments(city="x" * 101)
    with pytest.raises(ValidationError):
        ScheduleArguments(date="2026-02-30")
    with pytest.raises(ValidationError):
        ScheduleArguments(date="2026-1-2")
    with pytest.raises(ValidationError):
        ReminderArguments(text="  ", remind_at="2026-10-27T12:00:00")
    with pytest.raises(ValidationError):
        ScheduleArguments.model_validate({"date": "2026-10-26", "path": "/tmp/schedule.json"})
    aware_datetime = datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert ReminderArguments(text="work", remind_at=aware_datetime).remind_at == aware_datetime
    with pytest.raises(ValidationError):
        ReminderArguments(text="work", remind_at=1_793_102_400)


def test_weather_result_requires_fields_for_each_status() -> None:
    with pytest.raises(ValidationError):
        WeatherResult(status="ok", city="Paris")
    with pytest.raises(ValidationError):
        WeatherResult(status="ambiguous", candidates=[])


def test_weather_schema_enforces_status_requirements_and_candidate_count() -> None:
    from app.mcp_server.server import tool_catalog

    schema = next(tool.outputSchema for tool in tool_catalog() if tool.name == "get_weather")
    assert schema is not None
    validator = Draft202012Validator(schema)
    valid_ok = {
        "status": "ok",
        "city": "Paris",
        "country": "France",
        "temperature": 15.0,
        "weather_code": 1,
        "wind_speed": 2.0,
        "wind_unit": "km/h",
        "observed_at": "2026-10-26T12:00",
        "timezone": "Europe/Paris",
        "candidates": [],
    }
    valid_ambiguous = {
        "status": "ambiguous",
        "candidates": [
            {"name": "Springfield", "country": "USA", "region": "Illinois"},
            {"name": "Springfield", "country": "USA", "region": "Massachusetts"},
        ],
    }
    assert validator.is_valid(valid_ok)
    assert not validator.is_valid({"status": "ok"})
    assert validator.is_valid(valid_ambiguous)
    assert not validator.is_valid(
        {"status": "ambiguous", "candidates": valid_ambiguous["candidates"][:1]}
    )
    with pytest.raises(ValidationError):
        WeatherResult.model_validate({**valid_ok, "temperature": float("inf")})
    with pytest.raises(ValidationError):
        WeatherResult.model_validate({**valid_ok, "wind_speed": float("nan")})


def test_normalize_reminder_rejects_past_missing_timezone_and_offset_mismatch() -> None:
    now = datetime(2026, 3, 1, 12, tzinfo=UTC)
    base = {"text": "проверить"}
    with pytest.raises(ToolFailure) as past:
        normalize_reminder({**base, "remind_at": "2026-03-01T11:00:00+01:00"}, "Europe/Berlin", now)
    assert past.value.code == "past_time"
    with pytest.raises(ToolFailure) as no_zone:
        normalize_reminder({**base, "remind_at": "2026-03-01T15:00:00+01:00"}, None, now)
    assert no_zone.value.code == "timezone_required"
    with pytest.raises(ToolFailure) as mismatch:
        normalize_reminder({**base, "remind_at": "2026-03-01T15:00:00+03:00"}, "Europe/Berlin", now)
    assert mismatch.value.code == "timezone_mismatch"


@pytest.mark.parametrize(
    "remind_at",
    ["2026-10-25T02:30:00+01:00", "2026-10-25T02:30:00+02:00"],
)
def test_normalize_reminder_requests_clarification_for_dst_overlap(remind_at: str) -> None:
    with pytest.raises(ToolFailure) as raised:
        normalize_reminder(
            {"text": "DST", "remind_at": remind_at},
            "Europe/Berlin",
            datetime(2026, 10, 1, tzinfo=UTC),
        )
    assert raised.value.code == "ambiguous_time"


def test_normalize_reminder_requests_clarification_for_dst_gap() -> None:
    with pytest.raises(ToolFailure) as raised:
        normalize_reminder(
            {"text": "DST", "remind_at": "2026-03-29T02:30:00+01:00"},
            "Europe/Berlin",
            datetime(2026, 3, 1, tzinfo=UTC),
        )
    assert raised.value.code == "ambiguous_time"


def test_normalize_reminder_accepts_future_time_across_year_boundary() -> None:
    result = normalize_reminder(
        {"text": "New year", "remind_at": "2027-01-01T01:00:00+01:00"},
        "Europe/Berlin",
        datetime(2026, 12, 31, 23, 30, tzinfo=UTC),
    )
    assert result.remind_at.isoformat() == "2027-01-01T01:00:00+01:00"


def test_unknown_iana_zone_is_rejected() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        normalize_reminder(
            {"text": "проверить", "remind_at": "2027-01-01T12:00:00+03:00"},
            "Invalid/Unknown",
            datetime(2026, 10, 26, tzinfo=UTC),
        )


def test_clocks_return_aware_datetimes() -> None:
    frozen = datetime(2026, 10, 26, tzinfo=UTC)
    assert FrozenClock(frozen).now() == frozen
    assert SystemClock().now().utcoffset() is not None
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 10, 26))


@pytest.mark.asyncio
async def test_weather_not_found_does_not_request_forecast() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WeatherService(client).get_weather("Неизвестный город")
    assert result.status == "not_found"
    assert result.candidates == []
    assert result.temperature is None
    assert len(requests) == 1
    assert "geocoding" in requests[0].url.host


@pytest.mark.asyncio
async def test_weather_returns_candidates_for_ambiguous_city() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Springfield",
                        "country": "United States",
                        "admin1": "Illinois",
                        "latitude": 39.8,
                        "longitude": -89.6,
                    },
                    {
                        "name": "Springfield",
                        "country": "United States",
                        "admin1": "Massachusetts",
                        "latitude": 42.1,
                        "longitude": -72.6,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WeatherService(client).get_weather("Springfield")
    assert result.status == "ambiguous"
    assert [candidate.region for candidate in result.candidates] == ["Illinois", "Massachusetts"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_weather_success_filters_country_and_reads_current_conditions() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "geocoding" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"name": "Paris", "country": "France", "latitude": 48.8, "longitude": 2.3},
                        {
                            "name": "Paris",
                            "country": "United States",
                            "latitude": 33.7,
                            "longitude": -95.5,
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "timezone": "Europe/Paris",
                "current": {
                    "temperature_2m": 16.5,
                    "weather_code": 2,
                    "wind_speed_10m": 8.2,
                    "time": "2026-10-26T12:00",
                },
                "current_units": {"wind_speed_10m": "km/h"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WeatherService(client).get_weather(" Paris, France ")
    assert result.status == "ok"
    assert result.city == "Paris"
    assert result.country == "France"
    assert result.temperature == 16.5
    assert result.wind_unit == "km/h"
    assert result.timezone == "Europe/Paris"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_weather_retries_once_then_raises_safe_failure() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ConnectError("private network detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure) as raised:
            await WeatherService(client).get_weather("Казань")
    assert requests == 4
    assert raised.value.code == "weather_unavailable"
    assert "private network detail" not in raised.value.message


@pytest.mark.asyncio
async def test_weather_timeout_retries_once_then_raises_safe_failure() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("private timeout detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure) as raised:
            await WeatherService(client).get_weather("Казань")
    assert requests == 4
    assert raised.value.code == "weather_unavailable"
    assert "private timeout detail" not in raised.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [httpx.Response(200, json={"results": {"unexpected": "shape"}})],
        [
            httpx.Response(
                200,
                json={
                    "results": [
                        {"name": "Paris", "country": "France", "latitude": 1, "longitude": 2}
                    ]
                },
            ),
            httpx.Response(
                200, json={"timezone": "Europe/Paris", "current": {}, "current_units": {}}
            ),
        ],
        [httpx.Response(200, content=b"not-json")],
    ],
)
async def test_weather_invalid_api_responses_raise_safe_failure(
    responses: list[httpx.Response],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure) as raised:
            await WeatherService(client).get_weather("Paris")
    assert raised.value.code == "weather_unavailable"


@pytest.mark.asyncio
async def test_schedule_reads_empty_day_and_week_across_year_boundary(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "2026-12-28": [
                    {"start": "09:00", "end": "10:30", "title": "Лекция", "location": "Аудитория 1"}
                ],
                "2026-12-29": [],
                "2027-01-03": [],
            }
        ),
        encoding="utf-8",
    )
    service = ScheduleService(
        schedule_path,
        "Europe/Moscow",
        FrozenClock(datetime(2027, 1, 1, 12, tzinfo=UTC)),
    )
    empty = await service.get_schedule("2026-12-29")
    week = await service.current_week()
    assert empty.lessons == []
    assert week.start_date == "2026-12-28"
    assert week.end_date == "2027-01-03"
    assert week.timezone == "Europe/Moscow"
    assert week.days[0].lessons[0].title == "Лекция"
    assert week.days[0].lessons[0].model_dump() == {
        "start": "09:00",
        "end": "10:30",
        "title": "Лекция",
        "location": "Аудитория 1",
    }
    assert len(week.days) == 7


@pytest.mark.asyncio
async def test_schedule_rejects_invalid_date_before_reading_file(tmp_path):
    service = ScheduleService(tmp_path / "missing.json", "Europe/Moscow", SystemClock())
    for invalid in ("2026-2-3", "2026-02-30", "yesterday"):
        with pytest.raises(ToolFailure) as failure:
            await service.get_schedule(invalid)
        assert failure.value.code == "invalid_date"


async def test_default_schedule_day_uses_schedule_timezone_and_clock(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    path.write_text('{"2026-10-28": [], "2026-10-29": []}', encoding="utf-8")
    clock = FrozenClock(datetime(2026, 10, 28, 22, tzinfo=UTC))
    service = ScheduleService(path, "Europe/Moscow", clock)
    today = await service.get_schedule()
    explicit = await service.get_schedule("2026-10-28")
    assert today.date == "2026-10-29" and today.data_available
    assert explicit.date == "2026-10-28" and explicit.data_available
