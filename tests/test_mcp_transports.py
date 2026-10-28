import json
import sys
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from app.core.clock import FrozenClock
from app.mcp_server.__main__ import http_app
from app.mcp_server.config import ServerSettings
from app.mcp_server.server import build_server

SECRET = "transport-test-secret-0123456789abcdef"


def settings(schedule_path):
    return ServerSettings(
        postgres_password="database-password",
        mcp_shared_secret=SECRET,
        schedule_path=schedule_path,
        schedule_timezone="Europe/Moscow",
    )


def schedule_file(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text(
        json.dumps(
            {
                "2030-01-02": [
                    {
                        "start": "09:00",
                        "end": "10:30",
                        "title": "Распределённые системы",
                        "location": "Аудитория 101",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def weather_handler(request):
    if request.url.host == "geocoding-api.open-meteo.com":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": request.url.params["name"],
                        "country": "Россия",
                        "latitude": 55.75,
                        "longitude": 37.62,
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "current": {
                "temperature_2m": 8,
                "weather_code": 3,
                "wind_speed_10m": 4,
                "time": "2030-01-02T09:00",
            },
            "current_units": {"wind_speed_10m": "km/h"},
            "timezone": "Europe/Moscow",
        },
    )


async def never_acquire():
    raise AssertionError("untrusted mutation reached the database")


def fake_pool():
    pool = Mock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.acquire = Mock(side_effect=never_acquire)
    return pool


def stdio_source(schedule_path):
    geocoding = json.dumps(
        {
            "results": [
                {
                    "name": "Москва",
                    "country": "Россия",
                    "latitude": 55.75,
                    "longitude": 37.62,
                }
            ]
        }
    )
    forecast = json.dumps(
        {
            "current": {
                "temperature_2m": 8,
                "weather_code": 3,
                "wind_speed_10m": 4,
                "time": "2030-01-02T09:00",
            },
            "current_units": {"wind_speed_10m": "km/h"},
            "timezone": "Europe/Moscow",
        }
    )
    return f"""
import asyncio
import json
import httpx
from pathlib import Path
from mcp.server.stdio import stdio_server
from app.mcp_server.config import ServerSettings
from app.mcp_server.server import build_server

class Pool:
    async def fetchval(self, *args, **kwargs):
        return True
    def acquire(self):
        raise AssertionError("untrusted mutation reached the database")

def handler(request):
    if request.url.host == "geocoding-api.open-meteo.com":
        return httpx.Response(200, json=json.loads({geocoding!r}))
    return httpx.Response(200, json=json.loads({forecast!r}))

async def main():
    config = ServerSettings(
        postgres_password="db",
        mcp_shared_secret={SECRET!r},
        schedule_path=Path({str(schedule_path)!r}),
        schedule_timezone="Europe/Moscow",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        server = build_server(Pool(), config, http)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_stdio_transport_discovers_and_calls_server(tmp_path):
    path = schedule_file(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-c", stdio_source(path)])

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            resources = await session.list_resources()
            assert {tool.name for tool in tools.tools} == {
                "get_weather",
                "get_schedule",
                "get_schedule_week",
                "add_reminder",
            }
            assert [str(resource.uri) for resource in resources.resources] == [
                "schedule://current-week"
            ]

            schedule = await session.call_tool("get_schedule", {"date": "2030-01-02"})
            assert schedule.structuredContent is not None
            assert schedule.isError is not True
            assert schedule.structuredContent["lessons"][0]["title"] == "Распределённые системы"
            repeated = await session.call_tool("get_schedule", {"date": "2030-01-02"})
            assert repeated.structuredContent == schedule.structuredContent
            next_week = await session.call_tool("get_schedule_week", {"week_offset": 1})
            assert not next_week.isError and next_week.structuredContent is not None
            assert len(next_week.structuredContent["days"]) == 7

            weather = await session.call_tool("get_weather", {"city": "Москва"})
            assert weather.structuredContent is not None
            assert weather.isError is not True
            assert weather.structuredContent["temperature"] == 8.0

            week = await session.read_resource(AnyUrl("schedule://current-week"))
            content = week.contents[0]
            assert isinstance(content, types.TextResourceContents)
            week_data = json.loads(content.text)
            assert len(week_data["days"]) == 7

            extra_field = await session.call_tool(
                "get_schedule", {"date": "2030-01-02", "unexpected": True}
            )
            assert extra_field.isError is True
            untrusted_mutation = await session.call_tool(
                "add_reminder",
                {"text": "Тест", "remind_at": "2030-01-03T09:00:00+03:00"},
            )
            assert untrusted_mutation.isError is True


@pytest.mark.asyncio
async def test_streamable_http_transport_auth_and_mcp_contract(tmp_path):
    path = schedule_file(tmp_path)
    pool = fake_pool()
    transport = httpx.MockTransport(weather_handler)
    async with httpx.AsyncClient(transport=transport) as weather_http:
        server = build_server(
            pool,
            settings(path),
            weather_http,
            FrozenClock(datetime.fromisoformat("2030-01-01T22:00:00+00:00")),
        )
        app = http_app(server, SECRET, pool)

        async with app.router.lifespan_context(app):
            raw_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )
            async with raw_client:
                unauthorized = await raw_client.get("/mcp/")
                assert unauthorized.status_code == 401
                health = await raw_client.get("/health")
                assert health.status_code == 200

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {SECRET}", "X-User-Id": "42"},
            ) as client:
                async with streamable_http_client(url="http://test/mcp/", http_client=client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        resources = await session.list_resources()
                        assert len(tools.tools) == 4
                        assert len(resources.resources) == 1

                        schedule = await session.call_tool("get_schedule", {"date": "2030-01-02"})
                        assert schedule.structuredContent is not None
                        assert schedule.isError is not True
                        assert (
                            schedule.structuredContent["lessons"][0]["title"]
                            == "Распределённые системы"
                        )
                        weather = await session.call_tool("get_weather", {"city": "Москва"})
                        assert weather.structuredContent is not None
                        assert weather.isError is not True
                        assert weather.structuredContent["status"] == "ok"

                        default_weather = await session.call_tool("get_weather", {})
                        assert not default_weather.isError
                        assert default_weather.structuredContent is not None
                        assert default_weather.structuredContent["city"] == "Санкт-Петербург"
                        assert default_weather.structuredContent["requested_date"] == "2030-01-02"
                        default_schedule = await session.call_tool("get_schedule", {})
                        assert not default_schedule.isError
                        assert default_schedule.structuredContent is not None
                        assert default_schedule.structuredContent["date"] == "2030-01-02"
                        assert len(default_schedule.structuredContent["lessons"]) == 1

                        week = await session.read_resource(AnyUrl("schedule://current-week"))
                        content = week.contents[0]
                        assert isinstance(content, types.TextResourceContents)
                        assert len(json.loads(content.text)["days"]) == 7
                        malformed = await session.call_tool(
                            "get_schedule", {"date": "2030-01-02", "unexpected": True}
                        )
                        assert malformed.isError is True

                        missing_context = await session.call_tool(
                            "add_reminder",
                            {"text": "Тест", "remind_at": "2030-01-03T09:00:00+03:00"},
                        )
                        forged_context = await session.call_tool(
                            "add_reminder",
                            {"text": "Тест", "remind_at": "2030-01-03T09:00:00+03:00"},
                            meta={
                                "app_token": "wrong-token",
                                "user_id": 42,
                                "confirmation_id": "00000000-0000-0000-0000-000000000000",
                            },
                        )
                        assert missing_context.isError is True
                        assert forged_context.isError is True
                        pool.acquire.assert_not_called()
                        pool.fetchval.assert_awaited()
