import asyncio
import json
import socket
import sys
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import asyncpg
import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server.http import StarletteWithLifespan

from app.config import MCPTransport, Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, EventContext
from app.core.schemas.llm import LLMResponse, ToolDefinition
from app.core.schemas.mcp import ResourceContent, ResourceDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import PhotoToolResult, ToolFailure
from app.health import HealthState, health_result
from app.mcp.client import MCPClient
from app.mcp.composite import CompositeToolClient
from app.mcp.connection import create_math_tools_client, create_tool_clients
from app.services.agent_service import AgentService
from app.services.tool_execution import ToolExecutionService
from mcp_servers.math_tools.__main__ import http_app as math_http_app
from mcp_servers.math_tools.server import build_server as build_math_server
from mcp_servers.student_tools.__main__ import http_app as student_http_app
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server as build_student_server
from tests.fake_llm import ScriptedLLM
from tests.mcp_catalog import tool_catalog

SECRET = "composite-transport-secret-0123456789abcdef"
REMINDER_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMINDER_ARGS = {
    "text": "Проверить расписание",
    "remind_at": "2030-01-03T10:00:00+03:00",
}
STUDENT_TOOLS = {
    "get_weather",
    "get_schedule",
    "get_schedule_week",
    "add_reminder",
}
MATH_TOOLS = {"math_calculate", "math_solve", "math_plot"}


class FakeStudentConnection:
    def __init__(self, pool: "FakeStudentPool") -> None:
        self.pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None, None]:
        yield

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "FROM pending_confirmations" in query:
            return dict(self.pool.pending)
        if "FROM reminders WHERE idempotency_key" in query:
            return None
        if "INSERT INTO reminders" in query:
            self.pool.inserts += 1
            return {
                "id": 7,
                "text": REMINDER_ARGS["text"],
                "remind_at": datetime.fromisoformat(REMINDER_ARGS["remind_at"]),
                "timezone": "Europe/Moscow",
            }

        raise AssertionError("unexpected fake SQL query")


class FakeStudentPool:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.inserts = 0
        self.pending = {
            "id": UUID(REMINDER_ID),
            "user_id": 42,
            "arguments": dict(REMINDER_ARGS),
            "state": "executing",
            "confirmed_at": now,
            "expires_at": now + timedelta(days=2),
            "idempotency_key": f"reminder:{REMINDER_ID}",
            "timezone": "Europe/Moscow",
        }

    async def fetchval(self, query: str, *args: object, **kwargs: object) -> bool:
        return True

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[FakeStudentConnection, None]:
        yield FakeStudentConnection(self)


def weather_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "geocoding-api.open-meteo.com":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Москва",
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


class FakeToolClient:
    def __init__(
        self,
        name: str,
        *,
        tools: Sequence[str] = (),
        resources: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.available = bool(tools or resources)
        self.tools = {item: ToolDefinition(name=item) for item in tools}
        self.resources = {
            uri: ResourceDefinition(uri=uri, name=f"{name} resource") for uri in resources
        }
        self.read_only: set[str] = set()
        self.validated: list[tuple[str, dict[str, object]]] = []
        self.calls: list[tuple[str, dict[str, object], int | None, str | None]] = []
        self.starts = 0
        self.closes = 0
        self.start_error: Exception | None = None
        self.close_error: Exception | None = None
        self.outcome: dict[str, object] | PhotoToolResult | None = None

    async def start(self) -> bool:
        self.starts += 1
        if self.start_error is not None:
            self.available = False
            raise self.start_error
        self.available = bool(self.tools or self.resources)
        return self.available

    async def aclose(self) -> None:
        self.closes += 1
        self.available = False
        if self.close_error is not None:
            raise self.close_error

    def is_read_only(self, name: str) -> bool:
        return name in self.read_only

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        self.validated.append((name, dict(arguments)))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object] | PhotoToolResult:
        self.calls.append((name, dict(arguments), user_id, confirmation_id))
        if self.outcome is not None:
            return self.outcome
        return {"source": self.name, "tool": name}

    async def read_resource(self, uri: str) -> ResourceContent:
        if uri not in self.resources:
            raise ToolFailure("resource_unavailable", "Ресурс недоступен.")
        return ResourceContent(uri=uri, text=f"{self.name}:{uri}")


def test_aggregate_contains_unique_entries_from_available_clients() -> None:
    student = FakeToolClient(
        "student",
        tools=["get_schedule"],
        resources=["schedule://current-week"],
    )
    math = FakeToolClient(
        "math",
        tools=["math_calculate", "math_plot"],
        resources=["math://constants"],
    )
    composite = CompositeToolClient([student, math])

    assert list(composite.tools) == ["get_schedule", "math_calculate", "math_plot"]
    assert list(composite.resources) == [
        "schedule://current-week",
        "math://constants",
    ]

    student.read_only.add("get_schedule")
    assert composite.is_read_only("get_schedule") is True
    assert composite.is_read_only("math_calculate") is False

    composite.validate_arguments("get_schedule", {"date": "2026-11-16"})
    assert student.validated == [("get_schedule", {"date": "2026-11-16"})]
    assert math.validated == []


def test_unavailable_client_is_excluded_until_recovered() -> None:
    student = FakeToolClient("student", tools=["get_schedule"])
    math = FakeToolClient("math", tools=["math_calculate"])
    composite = CompositeToolClient([student, math])
    math.available = False

    assert list(composite.tools) == ["get_schedule"]
    assert composite.available is True

    math.available = True
    assert list(composite.tools) == ["get_schedule", "math_calculate"]

    student.available = False
    math.available = False
    assert composite.available is False
    assert composite.tools == {}


async def test_tool_name_collision_is_excluded_and_rejected() -> None:
    student = FakeToolClient("student", tools=["shared", "get_schedule"])
    math = FakeToolClient("math", tools=["shared", "math_calculate"])
    composite = CompositeToolClient([student, math])

    assert list(composite.tools) == ["get_schedule", "math_calculate"]
    assert composite.is_read_only("shared") is False

    with pytest.raises(ToolFailure) as validation:
        composite.validate_arguments("shared", {})
    with pytest.raises(ToolFailure) as execution:
        await composite.call_tool("shared", {})

    assert validation.value.code == "capability_conflict"
    assert execution.value.code == "capability_conflict"
    assert student.calls == []
    assert math.calls == []
    assert "shared" in student.tools
    assert "shared" in math.tools

    math.available = False
    assert "shared" in composite.tools
    assert await composite.call_tool("shared", {}) == {"source": "student", "tool": "shared"}


async def test_resource_uri_collision_is_excluded_and_rejected() -> None:
    student = FakeToolClient(
        "student",
        resources=["shared://resource", "schedule://current-week"],
    )
    math = FakeToolClient("math", resources=["shared://resource", "math://constants"])
    composite = CompositeToolClient([student, math])

    assert list(composite.resources) == [
        "schedule://current-week",
        "math://constants",
    ]

    with pytest.raises(ToolFailure) as collision:
        await composite.read_resource("shared://resource")

    assert collision.value.code == "capability_conflict"
    assert (await composite.read_resource("schedule://current-week")).text == (
        "student:schedule://current-week"
    )

    math.available = False
    assert "shared://resource" in composite.resources
    assert (await composite.read_resource("shared://resource")).text == "student:shared://resource"


async def test_reminder_call_goes_only_to_student_client() -> None:
    student = FakeToolClient("student", tools=["add_reminder"])
    math = FakeToolClient("math", tools=["math_calculate"])
    composite = CompositeToolClient([student, math])
    arguments = {
        "text": "Проверить расписание",
        "remind_at": "2026-11-16T11:00:00+03:00",
    }

    result = await composite.call_tool(
        "add_reminder",
        arguments,
        user_id=42,
        confirmation_id="confirmation-1",
    )

    assert result == {"source": "student", "tool": "add_reminder"}
    assert student.calls == [
        ("add_reminder", arguments, 42, "confirmation-1"),
    ]
    assert math.calls == []
    assert list(student.tools) == ["add_reminder"]


async def test_photo_result_from_owner_is_forwarded_unchanged() -> None:
    math = FakeToolClient("math", tools=["math_plot"])
    composite = CompositeToolClient([math])
    photo = PhotoToolResult(payload={"curves": ["sin(x)"], "range": [-10, 10]})
    math.outcome = photo

    result = await composite.call_tool("math_plot", {"functions": ["sin(x)"]})

    assert result is photo


async def test_start_failure_isolated_and_recovery_retries_only_unavailable_server() -> None:
    student = FakeToolClient("student", tools=["get_schedule"])
    math = FakeToolClient("math", tools=["math_calculate"])
    math.start_error = RuntimeError("math did not start")
    composite = CompositeToolClient([student, math])

    assert await composite.start() is True
    assert student.starts == 1
    assert math.starts == 1
    assert list(composite.tools) == ["get_schedule"]

    math.start_error = None
    assert await composite.recover() is True
    assert student.starts == 1
    assert math.starts == 2
    assert list(composite.tools) == ["get_schedule", "math_calculate"]

    assert await composite.recover() is False


async def test_all_servers_down_keeps_composite_unavailable() -> None:
    student = FakeToolClient("student", tools=["get_schedule"])
    math = FakeToolClient("math", tools=["math_calculate"])
    student.start_error = RuntimeError("student did not start")
    math.start_error = RuntimeError("math did not start")
    composite = CompositeToolClient([student, math])

    assert await composite.start() is False
    assert composite.available is False
    assert composite.tools == {}
    assert composite.resources == {}


async def test_close_failure_does_not_skip_other_clients() -> None:
    student = FakeToolClient("student", tools=["get_schedule"])
    math = FakeToolClient("math", tools=["math_calculate"])
    student.close_error = RuntimeError("client close failed")
    composite = CompositeToolClient([student, math])

    await composite.aclose()

    assert student.closes == 1
    assert math.closes == 1
    assert composite.available is False


async def test_health_reports_composite_available_while_any_server_is_up() -> None:
    student = FakeToolClient("student", tools=["get_schedule"])
    math = FakeToolClient("math", tools=["math_calculate"])
    composite = CompositeToolClient([student, math])
    state = HealthState(mcp=composite)

    _, body = await health_result(state)
    assert body["mcp"] == "available"

    math.available = False
    _, body = await health_result(state)
    assert body["mcp"] == "available"

    student.available = False
    _, body = await health_result(state)
    assert body["mcp"] == "unavailable"


async def test_unknown_tool_and_resource_are_rejected() -> None:
    composite = CompositeToolClient([FakeToolClient("student", tools=["get_schedule"])])

    with pytest.raises(ToolFailure) as tool:
        await composite.call_tool("missing", {})
    with pytest.raises(ToolFailure) as resource:
        await composite.read_resource("missing://resource")

    assert tool.value.code == "unknown_tool"
    assert resource.value.code == "resource_unavailable"


async def test_real_mcp_clients_keep_discovered_catalogs_through_composite() -> None:
    student_server = FastMCP("student")
    math_server = FastMCP("math")

    @student_server.tool(annotations={"readOnlyHint": True})
    async def get_schedule(date: str) -> dict[str, str]:
        return {"date": date}

    @student_server.resource("schedule://current-week")
    def current_week() -> str:
        return '{"start_date": "2026-11-16"}'

    @math_server.tool(annotations={"readOnlyHint": True})
    async def math_calculate(expression: str) -> dict[str, str]:
        return {"expression": expression, "result": "2"}

    student = MCPClient(Client(student_server))
    math = MCPClient(Client(math_server))
    composite = CompositeToolClient([student, math])

    try:
        assert await composite.start() is True
        assert list(composite.tools) == ["get_schedule", "math_calculate"]
        assert list(composite.resources) == ["schedule://current-week"]
        assert composite.is_read_only("get_schedule") is True

        composite.validate_arguments("math_calculate", {"expression": "1+1"})
        with pytest.raises(ToolFailure) as invalid:
            composite.validate_arguments("get_schedule", {"date": "2026-11-16", "extra": 1})

        assert invalid.value.code == "invalid_arguments"
        assert await composite.call_tool("math_calculate", {"expression": "1+1"}) == {
            "expression": "1+1",
            "result": "2",
        }
        assert (await composite.read_resource("schedule://current-week")).text == (
            '{"start_date": "2026-11-16"}'
        )
        assert list(student.tools) == ["get_schedule"]
        assert list(math.tools) == ["math_calculate"]
    finally:
        await composite.aclose()


def schedule_file(tmp_path: Path) -> Path:
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
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def student_stdio_source() -> str:
    return """
import asyncio
import os
from pathlib import Path

import httpx

from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server
from tests.test_composite_mcp import FakeStudentPool

async def main():
    settings = ServerSettings(
        postgres_password="unused",
        mcp_shared_secret=os.environ["MCP_SHARED_SECRET"],
        schedule_path=Path(os.environ["SCHEDULE_PATH"]),
        schedule_timezone="Europe/Moscow",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as http:
        server = build_server(FakeStudentPool(), settings, http)
        await server.run_async(transport="stdio")

asyncio.run(main())
"""


def student_stdio_client(schedule_path: Path) -> MCPClient:
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", student_stdio_source()],
        env={
            "MCP_SHARED_SECRET": SECRET,
            "SCHEDULE_PATH": str(schedule_path),
            "PYTHONPATH": str(PROJECT_ROOT),
        },
        cwd=str(PROJECT_ROOT),
        keep_alive=True,
    )
    sdk = Client(
        transport,
        name="student-composite-test",
        timeout=30,
        init_timeout=30,
    )
    return MCPClient(sdk, secret=SECRET)


def stdio_math_client() -> MCPClient:
    client = create_math_tools_client(
        Settings(
            bot_token="unused",
            postgres_password="unused",
            mcp_transport=MCPTransport.STDIO,
            mcp_shared_secret=SECRET,
            mcp_timeout=30,
        )
    )
    assert client is not None
    return client


@asynccontextmanager
async def serve_app(app: StarletteWithLifespan) -> AsyncGenerator[str, None]:
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
    task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if task.done():
                await task
            if time.monotonic() >= deadline:
                raise TimeoutError("test HTTP server did not start")
            await asyncio.sleep(0.02)

        yield f"http://127.0.0.1:{port}/mcp/"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def student_http_application(
    pool: FakeStudentPool,
    schedule_path: Path,
    weather_http: httpx.AsyncClient,
) -> StarletteWithLifespan:
    settings = ServerSettings(
        postgres_password="unused",
        mcp_shared_secret=SECRET,
        schedule_path=schedule_path,
        schedule_timezone="Europe/Moscow",
    )
    server = build_student_server(cast(asyncpg.Pool, pool), settings, weather_http)
    return student_http_app(server, SECRET, cast(asyncpg.Pool, pool))


async def assert_two_server_composite(
    composite: CompositeToolClient,
    *,
    student_pool: FakeStudentPool | None = None,
) -> None:
    assert await composite.start() is True
    assert set(composite.tools) == STUDENT_TOOLS | MATH_TOOLS
    assert set(composite.resources) == {"schedule://current-week"}
    assert composite.resources["schedule://current-week"].uri == "schedule://current-week"
    resource = await composite.read_resource("schedule://current-week")
    assert resource.mime_type == "application/json"
    assert json.loads(resource.text)["start_date"]

    catalog = {tool.name: tool for tool in tool_catalog()}
    for name in STUDENT_TOOLS:
        assert composite.tools[name].parameters == catalog[name].input_schema

    assert composite.is_read_only("get_weather") is True
    assert composite.is_read_only("add_reminder") is False
    assert composite.is_read_only("math_calculate") is True

    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="schedule-call",
                        name="get_schedule",
                        arguments={"date": "2030-01-02"},
                    ),
                    ToolCall(
                        id="math-call",
                        name="math_calculate",
                        arguments={"expression": "2+2"},
                    ),
                ],
            ),
            LLMResponse(content="Готово"),
        ]
    )
    agent = AgentService(
        llm,
        ToolExecutionService(composite),
        clock=FrozenClock(datetime(2030, 1, 2, 12, tzinfo=UTC)),
    )

    result = await agent.run(
        [ChatMessage(role=MessageRole.USER, content="Посчитай и покажи расписание")],
        EventContext(user_id=42, event_id="composite-transport", request_id="request"),
        timezone="Europe/Moscow",
    )

    assert result.decision is not AgentDecision.ERROR
    assert result.text == "Готово"
    assert len(llm.requests) == 2
    assert STUDENT_TOOLS | MATH_TOOLS <= {tool.name for tool in llm.requests[0].tools}

    reminder = await composite.call_tool(
        "add_reminder",
        REMINDER_ARGS,
        user_id=42,
        confirmation_id=REMINDER_ID,
    )
    assert isinstance(reminder, dict)
    assert reminder["created"] is True
    assert reminder["text"] == REMINDER_ARGS["text"]
    if student_pool is not None:
        assert student_pool.inserts == 1


async def test_two_real_servers_compose_over_stdio(tmp_path: Path) -> None:
    schedule_path = schedule_file(tmp_path)
    student = student_stdio_client(schedule_path)
    math = stdio_math_client()
    composite = CompositeToolClient([student, math])

    try:
        await assert_two_server_composite(composite)
    finally:
        await composite.aclose()


async def test_two_real_servers_compose_over_streamable_http(tmp_path: Path) -> None:
    schedule_path = schedule_file(tmp_path)
    student_pool = FakeStudentPool()

    async with httpx.AsyncClient(transport=httpx.MockTransport(weather_handler)) as weather_http:
        student_app = student_http_application(student_pool, schedule_path, weather_http)
        math_app = math_http_app(build_math_server(), SECRET)
        async with serve_app(student_app) as student_url, serve_app(math_app) as math_url:
            clients = create_tool_clients(
                Settings(
                    bot_token="unused",
                    postgres_password="unused",
                    mcp_transport=MCPTransport.STREAMABLE_HTTP,
                    mcp_shared_secret=SECRET,
                    mcp_server_url=student_url,
                    math_mcp_server_url=math_url,
                    mcp_timeout=30,
                )
            )
            try:
                await assert_two_server_composite(
                    clients.composite,
                    student_pool=student_pool,
                )
            finally:
                await clients.composite.aclose()
