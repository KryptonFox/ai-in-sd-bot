import hmac
import json
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import ValidationError

from app.core.clock import SystemClock
from app.core.schemas.tools import (
    ReminderArguments,
    ReminderResult,
    ScheduleArguments,
    ScheduleResult,
    ToolErrorResult,
    ToolFailure,
    WeatherArguments,
    WeatherResult,
)
from app.db.repositories.reminder_repo import create_confirmed_reminder
from app.mcp_server.schedule import ScheduleService
from app.mcp_server.weather import WeatherService

TOOL_MODELS = {
    "get_weather": (
        WeatherArguments,
        WeatherResult,
        "Текущая погода; неоднозначный город требует уточнения. "
        "В city передавай ровно название города из запроса на исходном языке, "
        "добавляя через запятую страну/регион только если пользователь сам их указал. "
        "Не переводи, не сокращай и не добавляй географические уточнения от себя.",
    ),
    "get_schedule": (ScheduleArguments, ScheduleResult, "Занятия на дату YYYY-MM-DD."),
    "add_reminder": (
        ReminderArguments,
        ReminderResult,
        "Создать напоминание только после подтверждения владельца.",
    ),
}


def tool_catalog() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema=args.model_json_schema(),
            outputSchema=output_schema(result),
            annotations=types.ToolAnnotations(
                readOnlyHint=name != "add_reminder", destructiveHint=False, idempotentHint=True
            ),
        )
        for name, (args, result, description) in TOOL_MODELS.items()
    ]


def output_schema(result):
    success = result.model_json_schema()
    error = ToolErrorResult.model_json_schema()
    definitions = {**success.pop("$defs", {}), **error.pop("$defs", {})}
    schema = {"type": "object", "anyOf": [success, error]}
    if definitions:
        schema["$defs"] = definitions
    return schema


def error_result(code: str, message: str) -> types.CallToolResult:
    payload = {"error": {"code": code, "message": message}}
    return types.CallToolResult(
        isError=True,
        structuredContent=payload,
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


def build_server(pool, settings, http: httpx.AsyncClient, clock=None) -> Server:
    clock = clock or SystemClock()
    weather = WeatherService(http)
    schedule = ScheduleService(settings.schedule_path, settings.schedule_timezone, clock)
    server = Server("itmo-student-tools")

    @server.list_tools()
    async def list_tools():
        return tool_catalog()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]):
        try:
            if name not in TOOL_MODELS:
                raise ToolFailure("unknown_tool", "Инструмент не разрешён.")
            arguments = TOOL_MODELS[name][0].model_validate(arguments).model_dump(mode="json")
            if name == "get_weather":
                result = await weather.get_weather(arguments["city"])
            elif name == "get_schedule":
                result = await schedule.get_schedule(arguments["date"])
            else:
                metadata = server.request_context.meta
                meta = metadata.model_dump() if metadata else {}
                token = meta.get("app_token", "")
                if not isinstance(token, str) or not hmac.compare_digest(
                    token, settings.mcp_shared_secret
                ):
                    raise ToolFailure("unauthorized", "Нужен доверенный контекст приложения.")
                user_id = meta.get("user_id")
                confirmation = meta.get("confirmation_id")
                if type(user_id) is not int or not isinstance(confirmation, str):
                    raise ToolFailure("unauthorized", "Нужен подтверждённый владелец.")
                result = await create_confirmed_reminder(
                    pool, confirmation, user_id, arguments, clock.now()
                )
            payload = result.model_dump(mode="json")
            return types.CallToolResult(
                structuredContent=payload,
                content=[
                    types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
                ],
            )
        except ToolFailure as error:
            return error_result(error.code, error.message)
        except (ValidationError, ValueError, TypeError):
            return error_result("invalid_arguments", "Параметры инструмента некорректны.")
        except Exception:
            return error_result("service_unavailable", "Инструмент временно недоступен.")

    @server.list_resources()
    async def list_resources():
        return [
            types.Resource(
                uri="schedule://current-week",
                name="Текущая неделя",
                mimeType="application/json",
                description="Расписание понедельник–воскресенье.",
            )
        ]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != "schedule://current-week":
            raise ValueError("Ресурс не разрешён.")
        try:
            payload = (await schedule.current_week()).model_dump_json()
        except Exception:
            payload = json.dumps(
                {"error": {"code": "schedule_unavailable", "message": "Расписание недоступно."}}
            )
        return [ReadResourceContents(content=payload, mime_type="application/json")]

    return server
