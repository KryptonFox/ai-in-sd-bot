import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from app.core.clock import FrozenClock
from app.core.schemas.tools import ScheduleResult, WeekSchedule
from app.db.connection import create_pool
from app.db.repositories.calendar_repo import load_snapshot
from app.services.calendar_import import CalendarImportService
from app.services.personal_schedule import PersonalScheduleService
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server
from tests.conftest import DatabaseFixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]

SECRET = "calendar-integration-secret-0123456789"
ONE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:first
DTSTART;TZID=Europe/Moscow:20260907T100000
DTEND;TZID=Europe/Moscow:20260907T113000
SUMMARY:Первая
END:VEVENT
END:VCALENDAR
""".encode()
TWO_EVENTS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:second
DTSTART;TZID=Europe/Moscow:20260908T100000
DTEND;TZID=Europe/Moscow:20260908T113000
SUMMARY:Вторая
END:VEVENT
BEGIN:VEVENT
UID:third
DTSTART;TZID=Europe/Moscow:20260909T100000
DTEND;TZID=Europe/Moscow:20260909T113000
SUMMARY:Третья
END:VEVENT
END:VCALENDAR
""".encode()


async def test_calendar_replacement_ownership_and_mcp_no_import(
    database: DatabaseFixture,
    tmp_path: Path,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    clock = FrozenClock(datetime(2026, 9, 1, 9, tzinfo=UTC))
    service = CalendarImportService(pool, clock)
    personal = PersonalScheduleService(pool, clock)
    owner = 810_001
    stranger = 810_002
    concurrent = 810_003

    try:
        first = await service.import_calendar(owner, "first.ics", ONE_EVENT)
        assert first.event_count == 1
        assert first.replaced is False
        assert await load_snapshot(pool, stranger) is None

        second = await service.import_calendar(owner, "second.ics", TWO_EVENTS)
        assert second.event_count == 2
        assert second.replaced is True
        replacement = await load_snapshot(pool, owner)
        assert replacement is not None
        assert {event.summary for event in replacement.events} == {"Вторая", "Третья"}

        day = await personal.get_schedule(owner, "2026-09-08")
        assert day is not None and day.lessons[0].title == "Вторая"
        missing = await personal.get_schedule(stranger, "2026-09-08")
        assert missing.data_available is False
        assert missing.is_free_day is None

        await asyncio.gather(
            service.import_calendar(concurrent, "first.ics", ONE_EVENT),
            service.import_calendar(concurrent, "second.ics", TWO_EVENTS),
        )
        concurrent_snapshot = await load_snapshot(pool, concurrent)
        assert concurrent_snapshot is not None
        assert {event.summary for event in concurrent_snapshot.events} in (
            {"Первая"},
            {"Вторая", "Третья"},
        )

        schedule_path = tmp_path / "schedule.json"
        schedule_path.write_text(json.dumps({}), encoding="utf-8")
        server_settings = ServerSettings(
            postgres_password=settings.postgres_password,
            mcp_shared_secret=SECRET,
            postgres_host=settings.postgres_host,
            postgres_port=settings.postgres_port,
            postgres_db=settings.postgres_db,
            postgres_user=settings.postgres_user,
            schedule_path=schedule_path,
            schedule_timezone="Europe/Moscow",
        )

        async with httpx.AsyncClient() as http:
            server = build_server(pool, server_settings, http, clock)
            async with Client(server) as client:
                personal_result = await client.call_tool(
                    "get_schedule",
                    {"date": "2026-09-08"},
                    meta={"app_token": SECRET, "user_id": owner},
                )
                missing_result = await client.call_tool(
                    "get_schedule",
                    {"date": "2026-09-08"},
                    meta={"app_token": SECRET, "user_id": stranger},
                )
                missing_week = await client.call_tool(
                    "get_schedule_week",
                    {"date": "2026-09-08"},
                    meta={"app_token": SECRET, "user_id": stranger},
                )
                missing_resource = await client.read_resource(
                    "schedule://current-week",
                    meta={"app_token": SECRET, "user_id": stranger},
                )
                denied = await client.call_tool(
                    "get_schedule",
                    {"date": "2026-09-08"},
                    meta={"app_token": "wrong", "user_id": owner},
                    raise_on_error=False,
                )

        personal_day = ScheduleResult.model_validate(personal_result.structured_content)
        absent_day = ScheduleResult.model_validate(missing_result.structured_content)
        absent_week = WeekSchedule.model_validate(missing_week.structured_content)
        absent_resource_week = WeekSchedule.model_validate_json(missing_resource[0].text)

        assert personal_day.lessons[0].title == "Вторая"
        assert absent_day.data_available is False
        assert all(item.data_available is False for item in absent_week.days)
        assert all(item.data_available is False for item in absent_resource_week.days)
        assert denied.is_error is True
    finally:
        await pool.close()
