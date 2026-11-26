import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastmcp import Client
from mcp import MCPError
from mcp.types import TextContent, TextResourceContents

from app.core.clock import FrozenClock
from app.core.schemas.calendar import CalendarSnapshot, StoredCalendarEvent
from app.core.schemas.tools import ScheduleResult, WeekSchedule
from app.mcp.client import MCPClient
from app.mcp.request_context import MCPRequestContext, bind_mcp_request
from app.services import personal_schedule as personal_schedule_module
from app.services.personal_schedule import PersonalScheduleService
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server

SECRET = "personal-calendar-test-secret-0123456789"
MOSCOW = "Europe/Moscow"
CLOCK = FrozenClock(datetime(2026, 10, 12, 9, tzinfo=UTC))


def personal_event() -> StoredCalendarEvent:
    zone = ZoneInfo(MOSCOW)

    return StoredCalendarEvent(
        uid="personal",
        summary="Личная пара",
        location="Аудитория 5",
        starts_at=datetime(2026, 10, 12, 10, tzinfo=zone),
        ends_at=datetime(2026, 10, 12, 11, 30, tzinfo=zone),
        timezone=MOSCOW,
    )


def personal_snapshot(
    user_id: int = 1,
    events: tuple[StoredCalendarEvent, ...] = (),
) -> CalendarSnapshot:
    return CalendarSnapshot(
        user_id=user_id,
        timezone=MOSCOW,
        source_name="schedule.ics",
        imported_at=datetime(2026, 10, 1, tzinfo=UTC),
        events=events,
    )


def settings_for(tmp_path: Path) -> ServerSettings:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "2026-10-12": [
                    {
                        "start": "09:00",
                        "end": "10:00",
                        "title": "Общая пара",
                        "location": "Корпус",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return ServerSettings(
        postgres_password="unused",
        mcp_shared_secret=SECRET,
        schedule_path=schedule_path,
        schedule_timezone=MOSCOW,
    )


def patch_schedule(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: dict[int, CalendarSnapshot],
    *,
    failure: Exception | None = None,
) -> None:
    async def load_snapshot(pool: object, user_id: int) -> CalendarSnapshot | None:
        if failure is not None:
            raise failure
        return snapshots.get(user_id)

    async def get_timezone(pool: object, user_id: int) -> str:
        return MOSCOW

    monkeypatch.setattr(personal_schedule_module, "load_snapshot", load_snapshot)
    monkeypatch.setattr(personal_schedule_module, "get_timezone", get_timezone)


def resource_text(contents: Sequence[object]) -> str:
    return cast(TextResourceContents, contents[0]).text


def error_payload(result: object) -> dict[str, object]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return cast(dict[str, object], structured)

    content = getattr(result, "content", None)
    assert isinstance(content, list) and content
    text_content = cast(TextContent, content[0])
    payload = json.loads(text_content.text)
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


@asynccontextmanager
async def open_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshots: dict[int, CalendarSnapshot],
    *,
    failure: Exception | None = None,
) -> AsyncGenerator[Client]:
    patch_schedule(monkeypatch, snapshots, failure=failure)

    async with httpx.AsyncClient() as http:
        server = build_server(MagicMock(), settings_for(tmp_path), http, CLOCK)
        async with Client(server) as client:
            yield client


async def test_global_json_fallback_stays_for_calls_without_scoped_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_client(tmp_path, monkeypatch, {}) as client:
        day = await client.call_tool("get_schedule", {"date": "2026-10-12"})
        week = await client.call_tool("get_schedule_week", {"date": "2026-10-12"})
        resource = await client.read_resource("schedule://current-week")

    day_result = ScheduleResult.model_validate(day.structured_content)
    week_result = WeekSchedule.model_validate(week.structured_content)
    resource_week = WeekSchedule.model_validate_json(resource_text(resource))

    assert day_result.data_available is True
    assert day_result.lessons[0].title == "Общая пара"
    assert week_result.start_date == "2026-10-12"
    assert resource_week.days[0].lessons[0].title == "Общая пара"


async def test_owner_metadata_reads_personal_day_week_and_current_week_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = {"app_token": SECRET, "user_id": 1}
    snapshots = {1: personal_snapshot(events=(personal_event(),))}

    async with open_client(tmp_path, monkeypatch, snapshots) as client:
        day = await client.call_tool("get_schedule", {"date": "2026-10-12"}, meta=meta)
        week = await client.call_tool(
            "get_schedule_week",
            {"date": "2026-10-12"},
            meta=meta,
        )
        resource = await client.read_resource("schedule://current-week", meta=meta)

    day_result = ScheduleResult.model_validate(day.structured_content)
    week_result = WeekSchedule.model_validate(week.structured_content)
    resource_week = WeekSchedule.model_validate_json(resource_text(resource))

    assert day_result.lessons[0].title == "Личная пара"
    assert week_result.days[0].lessons[0].title == "Личная пара"
    assert resource_week.start_date == "2026-10-12"
    assert resource_week.days[0].lessons[0].title == "Личная пара"


async def test_other_owner_without_import_gets_typed_empty_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = {"app_token": SECRET, "user_id": 2}
    snapshots = {1: personal_snapshot(events=(personal_event(),))}

    async with open_client(tmp_path, monkeypatch, snapshots) as client:
        day = await client.call_tool("get_schedule", {"date": "2026-10-12"}, meta=meta)
        week = await client.call_tool("get_schedule_week", {"date": "2026-10-12"}, meta=meta)
        resource = await client.read_resource("schedule://current-week", meta=meta)

    day_result = ScheduleResult.model_validate(day.structured_content)
    week_result = WeekSchedule.model_validate(week.structured_content)
    resource_week = WeekSchedule.model_validate_json(resource_text(resource))

    assert day_result.data_available is False
    assert day_result.is_free_day is None
    assert day_result.lessons == []
    assert all(item.data_available is False for item in week_result.days)
    assert week_result.free_days == []
    assert all(item.data_available is False for item in resource_week.days)


async def test_imported_empty_day_differs_from_missing_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = {"app_token": SECRET, "user_id": 1}
    snapshots = {1: personal_snapshot()}

    async with open_client(tmp_path, monkeypatch, snapshots) as client:
        result = await client.call_tool("get_schedule", {"date": "2026-10-13"}, meta=meta)

    day_result = ScheduleResult.model_validate(result.structured_content)
    assert day_result.data_available is True
    assert day_result.is_free_day is True


@pytest.mark.parametrize(
    "meta",
    [
        {"app_token": "wrong", "user_id": 1},
        {"app_token": SECRET, "user_id": "1"},
        {"app_token": SECRET, "user_id": True},
        {"app_token": SECRET},
        {"user_id": 1},
        {"confirmation_id": "confirmation"},
    ],
)
async def test_invalid_claimed_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meta: dict[str, object],
) -> None:
    snapshots = {1: personal_snapshot(events=(personal_event(),))}

    async with open_client(tmp_path, monkeypatch, snapshots) as client:
        result = await client.call_tool(
            "get_schedule",
            {"date": "2026-10-12"},
            meta=meta,
            raise_on_error=False,
        )

    assert result.is_error is True
    error = cast(dict[str, object], error_payload(result)["error"])
    assert error["code"] == "unauthorized"


async def test_invalid_resource_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_client(tmp_path, monkeypatch, {}) as client:
        with pytest.raises(MCPError, match="доверенный контекст"):
            await client.read_resource(
                "schedule://current-week",
                meta={"app_token": "wrong", "user_id": 1},
            )


async def test_read_failure_stays_a_typed_error_not_an_empty_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = {"app_token": SECRET, "user_id": 1}

    async with open_client(
        tmp_path,
        monkeypatch,
        {},
        failure=RuntimeError("database down"),
    ) as client:
        result = await client.call_tool(
            "get_schedule",
            {"date": "2026-10-12"},
            meta=meta,
            raise_on_error=False,
        )

    assert result.is_error is True
    error = cast(dict[str, object], error_payload(result)["error"])
    assert error["code"] == "schedule_unavailable"


async def test_normal_mcp_client_returns_no_import_state_for_day_week_and_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_schedule(monkeypatch, {1: personal_snapshot(events=(personal_event(),))})

    async with httpx.AsyncClient() as http:
        server = build_server(MagicMock(), settings_for(tmp_path), http, CLOCK)
        mcp = MCPClient(Client(server), secret=SECRET)
        assert await mcp.start() is True

        try:
            with bind_mcp_request(MCPRequestContext(user_id=2)):
                day = await mcp.call_tool("get_schedule", {"date": "2026-10-12"})
                week = await mcp.call_tool("get_schedule_week", {"date": "2026-10-12"})
                resource = await mcp.read_resource("schedule://current-week")
        finally:
            await mcp.aclose()

    day_result = ScheduleResult.model_validate(day)
    week_result = WeekSchedule.model_validate(week)
    resource_week = WeekSchedule.model_validate_json(resource.text)

    assert day_result.data_available is False
    assert all(item.data_available is False for item in week_result.days)
    assert all(item.data_available is False for item in resource_week.days)


async def test_notification_interface_distinguishes_missing_import_from_free_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_schedule(monkeypatch, {})
    service = PersonalScheduleService(MagicMock(), CLOCK, MOSCOW)

    assert await service.day_schedule(1, date(2026, 10, 12)) is None

    empty = await service.get_schedule(1, "2026-10-12")
    assert empty.data_available is False
    assert empty.is_free_day is None

    patch_schedule(monkeypatch, {1: personal_snapshot()})
    free = await service.get_schedule(1, "2026-10-12")
    assert free.data_available is True
    assert free.is_free_day is True
