import asyncio
from collections.abc import Awaitable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from app.core.ports import ToolClient
from app.core.schemas.knowledge import MaterialCatalog
from app.core.schemas.tools import ScheduleResult, WeatherStatus
from app.core.trace import trace_step
from app.db.repositories.reminder_repo import reminders_between
from app.mcp.request_context import MCPRequestContext, bind_mcp_request
from app.services.notifications.contracts import BriefingContent, BriefingContext, BriefingSection
from mcp_servers.student_tools.geocoding import GeoLocation
from mcp_servers.student_tools.weather import WeatherService


class BriefingSources:
    def __init__(self, pool: asyncpg.Pool, student: ToolClient, weather: WeatherService) -> None:
        self._pool = pool
        self._student = student
        self._weather = weather

    async def __call__(self, context: BriefingContext) -> BriefingContent:
        with bind_mcp_request(MCPRequestContext(context.user_id, reminder_allowed=False)):
            sections = await asyncio.gather(
                self._section("Расписание", self._schedule(context)),
                self._section("Напоминания", self._reminders(context)),
                self._section("Погода", self._current_weather(context)),
                self._section("Учебный фокус", self._focus(context)),
            )
        return BriefingContent(tuple(sections))

    @staticmethod
    async def _section(title: str, compute: Awaitable[BriefingSection]) -> BriefingSection:
        try:
            async with asyncio.timeout(8):
                return await compute
        except TimeoutError:
            return BriefingSection(title, problem="Источник не ответил вовремя.")
        except Exception:
            return BriefingSection(title, problem="Источник временно недоступен.")

    async def _schedule(self, context: BriefingContext) -> BriefingSection:
        with trace_step("mcp", "call_tool", tool_name="get_schedule"):
            raw = await self._student.call_tool(
                "get_schedule", {"date": context.local_date.isoformat()}
            )
            schedule = ScheduleResult.model_validate(raw)
        if not schedule.data_available:
            return BriefingSection(
                "Расписание", problem="На эту дату личное расписание не загружено."
            )
        if not schedule.lessons:
            return BriefingSection("Расписание", text="Занятий нет.")
        lines = [
            f"{lesson.start}–{lesson.end} {lesson.title}"
            + (f" · {lesson.location}" if lesson.location else "")
            for lesson in schedule.lessons[:12]
        ]
        return BriefingSection("Расписание", text="\n".join(lines))

    async def _reminders(self, context: BriefingContext) -> BriefingSection:
        zone = ZoneInfo(context.timezone)
        start = datetime.combine(context.local_date, time.min, tzinfo=zone)
        end = datetime.combine(context.local_date + timedelta(days=1), time.min, tzinfo=zone)
        with trace_step("repository", "read_reminders"):
            reminders = await reminders_between(self._pool, context.user_id, start, end)
        lines = [f"{item.remind_at.astimezone(zone):%H:%M} {item.text}" for item in reminders]
        return BriefingSection(
            "Напоминания", text="\n".join(lines) if lines else "Напоминаний нет."
        )

    async def _current_weather(self, context: BriefingContext) -> BriefingSection:
        city = context.city
        with trace_step("weather", "get_current"):
            weather = await self._weather.current_weather_at(
                GeoLocation(
                    name=city.name,
                    country=city.country,
                    admin1=city.region,
                    latitude=city.latitude,
                    longitude=city.longitude,
                )
            )
        if weather.status is not WeatherStatus.OK or weather.temperature is None:
            return BriefingSection("Погода", problem="Прогноз недоступен.")
        return BriefingSection(
            "Погода",
            text=f"{city.name} · {weather.temperature:+g} °C · {weather.condition}",
        )

    async def _focus(self, context: BriefingContext) -> BriefingSection:
        with trace_step("mcp", "read_resource", tool_name="materials://catalog"):
            resource = await self._student.read_resource("materials://catalog")
            catalog = MaterialCatalog.model_validate_json(resource.text)
        documents = sorted(
            (document for document in catalog.documents if document.sections),
            key=lambda document: document.source_id,
        )
        if not documents:
            return BriefingSection("Учебный фокус", problem="В каталоге пока нет материалов.")
        position = context.local_date.toordinal()
        document = documents[position % len(documents)]
        section = document.sections[position // len(documents) % len(document.sections)]
        return BriefingSection(
            "Учебный фокус",
            text=f"{document.lecture_title} · {section.title}\n{section.citation}",
        )
