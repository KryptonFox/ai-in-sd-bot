import hmac
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import asyncpg
import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.context import Context
from pydantic import ValidationError

from app.core.clock import Clock, SystemClock
from app.core.schemas.tools import (
    DEFAULT_WEATHER_CITY,
    CityName,
    ISODate,
    ReminderArguments,
    ReminderResult,
    ReminderText,
    ScheduleArguments,
    ScheduleResult,
    ScheduleWeekArguments,
    ToolFailure,
    WeatherArguments,
    WeatherResult,
    WeekOffset,
    WeekSchedule,
)
from app.db.repositories.reminder_repo import create_confirmed_reminder
from app.services.personal_schedule import PersonalScheduleService
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.knowledge import register_knowledge_tools
from mcp_servers.student_tools.schedule import ScheduleService
from mcp_servers.student_tools.weather import WeatherService
from mcp_servers.tool_errors import raise_tool_error

WEATHER_DESCRIPTION = (
    "Текущая погода и дневной прогноз: температура, осадки, ветер. "
    "По умолчанию — Санкт-Петербург и сегодня; не запрашивай эти параметры без необходимости. "
    "Для завтра, послезавтра или другой даты передавай date=YYYY-MM-DD; горизонт 16 дней. "
    "Используй также для вопросов про зонт, дождь и одежду. "
    "Неоднозначное место требует уточнения. "
    "В city передавай название в нормальной форме на исходном языке: в Казани → Казань, "
    "в Нижнем Новгороде → Нижний Новгород. "
    "Добавляй через запятую страну/регион только если пользователь сам их указал. "
    "Не переводи, не сокращай и не добавляй географические уточнения от себя."
)


def _request_meta(context: Context) -> dict[str, Any]:
    request_context = context.request_context
    if request_context is None:
        return {}

    return request_context.meta or {}


def _reminder_owner(context: Context, shared_secret: str) -> tuple[int, str]:
    meta = _request_meta(context)
    token = meta.get("app_token", "")
    if not isinstance(token, str) or not hmac.compare_digest(token, shared_secret):
        raise ToolFailure("unauthorized", "Нужен доверенный контекст приложения.")

    user_id = meta.get("user_id")
    confirmation = meta.get("confirmation_id")
    if type(user_id) is not int or not isinstance(confirmation, str):
        raise ToolFailure("unauthorized", "Нужен подтверждённый владелец.")

    return user_id, confirmation


def _schedule_owner(context: Context, shared_secret: str) -> int | None:
    meta = _request_meta(context)
    if not {"app_token", "user_id", "confirmation_id"} & meta.keys():
        return None

    token = meta.get("app_token")
    if (
        not shared_secret
        or not isinstance(token, str)
        or not hmac.compare_digest(token, shared_secret)
    ):
        raise ToolFailure("unauthorized", "Нужен доверенный контекст приложения.")

    user_id = meta.get("user_id")
    if type(user_id) is not int or user_id <= 0:
        raise ToolFailure("unauthorized", "Нужен доверенный владелец.")

    return user_id


@contextmanager
def _translate_tool_errors() -> Generator[None, None, None]:
    try:
        yield
    except ToolFailure as error:
        raise_tool_error(error.code, error.message)
    except (ValidationError, ValueError, TypeError):
        raise_tool_error("invalid_arguments", "Параметры инструмента некорректны.")
    except Exception:
        raise_tool_error("service_unavailable", "Инструмент временно недоступен.")


def build_server(
    pool: asyncpg.Pool,
    settings: ServerSettings,
    http: httpx.AsyncClient,
    clock: Clock | None = None,
) -> FastMCP:
    clock = clock or SystemClock()
    weather = WeatherService(http, clock=clock)
    schedule = ScheduleService(settings.schedule_path, settings.schedule_timezone, clock)
    personal_schedule = PersonalScheduleService(pool, clock, settings.schedule_timezone)
    server = FastMCP("student-tools", strict_input_validation=True, mask_error_details=True)

    @server.tool(description=WEATHER_DESCRIPTION, annotations={"readOnlyHint": True})
    async def get_weather(
        city: CityName = DEFAULT_WEATHER_CITY,
        date: ISODate | None = None,
    ) -> WeatherResult:
        with _translate_tool_errors():
            arguments = WeatherArguments(city=city, date=date)
            return await weather.get_weather(arguments.city, arguments.date)

    @server.tool(
        description=(
            "Занятия на дату YYYY-MM-DD. Если дата не указана — сегодня в часовом поясе расписания."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_schedule(context: Context, date: ISODate | None = None) -> ScheduleResult:
        with _translate_tool_errors():
            arguments = ScheduleArguments(date=date)
            owner_id = _schedule_owner(context, settings.mcp_shared_secret)
            if owner_id is None:
                return await schedule.get_schedule(arguments.date)

            return await personal_schedule.get_schedule(owner_id, arguments.date)

    @server.tool(
        description=(
            "Расписание на всю неделю (понедельник–воскресенье), содержащую date=YYYY-MM-DD. "
            "Для следующей недели передавай week_offset=1 без date: календарь вычисляет сервер. "
            "Текущая неделя также доступна через ресурс schedule://current-week."
        ),
        annotations={"readOnlyHint": True},
    )
    async def get_schedule_week(
        context: Context,
        date: ISODate | None = None,
        week_offset: WeekOffset = 0,
    ) -> WeekSchedule:
        with _translate_tool_errors():
            arguments = ScheduleWeekArguments(date=date, week_offset=week_offset)
            owner_id = _schedule_owner(context, settings.mcp_shared_secret)

            if owner_id is None:
                if arguments.date is not None:
                    return await schedule.week_containing(arguments.date)
                return await schedule.relative_week(arguments.week_offset)

            if arguments.date is not None:
                return await personal_schedule.week_containing(owner_id, arguments.date)
            return await personal_schedule.relative_week(owner_id, arguments.week_offset)

    @server.tool(
        description="Создать напоминание только после подтверждения владельца.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    async def add_reminder(
        text: ReminderText,
        remind_at: datetime,
        context: Context,
    ) -> ReminderResult:
        with _translate_tool_errors():
            arguments = ReminderArguments(text=text, remind_at=remind_at)
            user_id, confirmation = _reminder_owner(context, settings.mcp_shared_secret)
            return await create_confirmed_reminder(
                pool,
                confirmation,
                user_id,
                arguments.model_dump(mode="json"),
                clock.now(),
            )

    @server.resource(
        "schedule://current-week",
        name="Текущая неделя",
        description="Расписание текущей недели целиком, понедельник–воскресенье.",
        mime_type="application/json",
    )
    async def current_week(context: Context) -> str:
        try:
            owner_id = _schedule_owner(context, settings.mcp_shared_secret)
            if owner_id is None:
                week = await schedule.current_week()
            else:
                week = await personal_schedule.current_week(owner_id)
            return week.model_dump_json()
        except ResourceError:
            raise
        except ToolFailure as error:
            raise ResourceError(error.message) from None
        except Exception:
            raise ResourceError("Расписание временно недоступно.") from None

    register_knowledge_tools(server, settings, http)
    return server
