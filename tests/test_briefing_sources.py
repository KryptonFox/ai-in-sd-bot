from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.schemas.calendar import empty_day_schedule
from app.core.schemas.mcp import ResourceContent
from app.core.schemas.tools import ToolFailure
from app.db.notification_records import CityRecord
from app.mcp.request_context import current_mcp_request
from app.services import briefing
from app.services.notifications.contracts import BriefingContext
from mcp_servers.student_tools.geocoding import GeoLocation


@pytest.mark.parametrize("data_available", [True, False])
async def test_briefing_preserves_valid_sections_and_trusted_context(
    monkeypatch: pytest.MonkeyPatch, data_available: bool
) -> None:
    now = datetime(2026, 11, 26, 5, tzinfo=UTC)
    city = CityRecord("Казань", "Россия", "Татарстан", 55.8, 49.1)
    owner = BriefingContext(101, uuid4(), date(2026, 11, 26), "Europe/Moscow", city, now, now)
    student = MagicMock()

    async def get_schedule(name: str, params: dict[str, str]) -> dict[str, object]:
        assert name == "get_schedule"
        assert params == {"date": "2026-11-26"}
        context = current_mcp_request()
        assert context is not None
        assert context.user_id == owner.user_id
        assert not context.web_search_allowed
        assert not context.reminder_allowed
        if not data_available:
            return empty_day_schedule(
                date.fromisoformat(params["date"]),
                "Europe/Moscow",
            ).model_dump(mode="json")
        return {"date": "2026-11-26", "lessons": [], "data_available": True}

    resource = ResourceContent(
        uri="materials://catalog",
        mime_type="application/json",
        text="""{"documents":[{
            "source_id":"lecture","title":"Лекция","subject_id":"course",
            "subject_title":"Курс","lecture_id":"intro","lecture_title":"Введение",
            "summary":"Что изучаем",
            "sections":[{"section_id":"first","title":"Тема","citation":"materials://lecture#first"}]
        }]}""",
    )
    student.call_tool = AsyncMock(side_effect=get_schedule)
    student.read_resource = AsyncMock(return_value=resource)
    weather = MagicMock()
    weather.current_weather_at = AsyncMock(side_effect=ToolFailure("weather_unavailable"))
    reminders = AsyncMock(return_value=[])
    monkeypatch.setattr(briefing, "reminders_between", reminders)
    pool = MagicMock()

    content = await briefing.BriefingSources(pool, student, weather)(owner)
    sections = {section.title: section for section in content.sections}

    assert current_mcp_request() is None
    assert sections["Погода"].problem is not None
    assert sections["Напоминания"].text == "Напоминаний нет."
    assert "materials://lecture#first" in (sections["Учебный фокус"].text or "")
    assert bool(sections["Расписание"].text) is data_available
    assert bool(sections["Расписание"].problem) is not data_available
    weather_call = weather.current_weather_at.await_args
    assert weather_call is not None
    location = weather_call.args[0]
    assert isinstance(location, GeoLocation)
    assert (location.latitude, location.longitude) == (city.latitude, city.longitude)
    reminder_call = reminders.await_args
    assert reminder_call is not None
    assert reminder_call.args[1] == owner.user_id
