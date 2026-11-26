import asyncio
import base64
import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from aiogram.types import Chat, Message, User
from fastmcp import Client, FastMCP
from mcp import types
from PIL import Image

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, AgentResult, EventContext
from app.core.schemas.llm import FinishReason, LLMResponse
from app.core.schemas.mcp import ResourceContent
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import PhotoToolResult, ToolImage
from app.core.schemas.user import UserSettings
from app.handlers import commands as command_handlers
from app.handlers.commands import ROLE_ICONS, get_roles_keyboard
from app.mcp.client import MCPClient
from app.modes.definitions import EVERYDAY_MODE, EVERYDAY_PARTS
from app.modes.registry import registry
from app.prompts.builder import build_prompt
from app.prompts.policy import SAFETY_TASK, TRUST_CONTEXT
from app.prompts.telegram import TELEGRAM_FORMATTING_PROMPT
from app.services import dialog_service as dialog_service_module
from app.services.agent_service import AgentService
from app.services.dialog_service import DialogService
from app.services.tool_execution import READ_RESOURCE_FUNCTION, ToolExecutionService
from app.telegram import get_default_bot_commands
from mcp_servers.math_tools.server import build_server as build_math_server
from tests.fake_llm import ScriptedLLM
from tests.mcp_catalog import resource_catalog, tool_catalog

EVALUATION_TIME = datetime(2026, 10, 12, 9, tzinfo=UTC)
CONTEXT = EventContext(user_id=42, event_id="everyday-event", request_id="everyday-request")
RESOURCE_URI = "schedule://current-week"
LEGACY_TOOL_NAMES = {
    "get_weather",
    "get_schedule",
    "get_schedule_week",
    "add_reminder",
}


def make_message(text: str) -> Message:
    return Message(
        message_id=12,
        date=EVALUATION_TIME,
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Student"),
        text=text,
    )


def tool_call(name: str, arguments: dict[str, object], identifier: str = "call-1") -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=arguments)


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", finish_reason=FinishReason.TOOL_CALLS, tool_calls=list(calls))


def everyday_messages(text: str) -> list[ChatMessage]:
    return [
        *EVERYDAY_MODE.build_initial_messages(text),
        ChatMessage(role=MessageRole.USER, content=text),
    ]


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def math_plot_payload(png: bytes) -> PhotoToolResult:
    payload: dict[str, object] = {
        "status": "ok",
        "functions": [{"expression": "x^2", "segments": 1, "discontinuities": []}],
        "variable": "x",
        "x_min": -10.0,
        "x_max": 10.0,
        "samples": 401,
        "png_bytes": len(png),
        "detail": "plot completed",
    }
    return PhotoToolResult(
        payload=payload,
        images=[ToolImage(data=base64.b64encode(png).decode("ascii"))],
    )


def weather_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "city": "Казань",
        "country": "Россия",
        "temperature": 5.0,
        "weather_code": 3,
        "wind_speed": 4.2,
        "wind_unit": "km/h",
        "observed_at": "2026-10-12T09:00",
        "timezone": "Europe/Moscow",
        "candidates": [],
    }


class EverydayToolClient(MCPClient):
    def __init__(self, math_tools: Sequence[types.Tool]) -> None:
        super().__init__(Client(FastMCP("everyday-test")))
        self.load_catalog(
            [tool for tool in tool_catalog() if tool.name in LEGACY_TOOL_NAMES] + list(math_tools)
        )
        self.load_resources(
            [resource for resource in resource_catalog() if str(resource.uri) == RESOURCE_URI]
        )
        self.tool_calls = AsyncMock(return_value=weather_payload())
        self.resource_reads = AsyncMock(
            return_value=ResourceContent(RESOURCE_URI, '{"days": [{"date": "2026-10-12"}]}')
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object] | PhotoToolResult:
        return await self.tool_calls(name, arguments)

    async def read_resource(self, uri: str) -> ResourceContent:
        return await self.resource_reads(uri)


def make_agent(
    client: EverydayToolClient,
    responses: Sequence[LLMResponse],
    *,
    confirmations=None,
    max_calls: int = 5,
) -> tuple[AgentService, ScriptedLLM, EverydayToolClient]:
    llm = ScriptedLLM(responses)
    agent = AgentService(
        llm,
        ToolExecutionService(client, confirmations, max_calls=max_calls),
        clock=FrozenClock(EVALUATION_TIME),
        max_llm_steps=4,
    )
    return agent, llm, client


async def discover_math_tools() -> list[types.Tool]:
    async with Client(build_math_server()) as client:
        return await client.list_tools()


@pytest.fixture(scope="module")
def math_tools() -> list[types.Tool]:
    return asyncio.run(discover_math_tools())


def test_everyday_registry_and_help_keep_study_default() -> None:
    assert EVERYDAY_MODE.name == "Повседневный помощник"
    assert EVERYDAY_MODE.command == "everyday"
    assert "погоде" in EVERYDAY_MODE.description
    assert registry.get("everyday") is EVERYDAY_MODE
    assert registry.get_or_default("unknown").id == "study"
    assert registry.get_or_default("everyday").id == "everyday"
    assert registry.list_modes()[0].id == "study"
    assert "everyday" in registry.commands()

    help_text = registry.format_help()
    assert "/everyday" in help_text
    assert "Повседневный помощник" in help_text


def test_everyday_uses_existing_rctf_parts_and_shared_rules() -> None:
    prompt = EVERYDAY_MODE.system_prompt
    assert prompt == build_prompt(EVERYDAY_PARTS)
    assert TELEGRAM_FORMATTING_PROMPT in prompt
    assert prompt.count(SAFETY_TASK) == 1
    assert prompt.count(TRUST_CONTEXT) == 1

    positions = [prompt.index(heading) for heading in ("Role:", "Context:", "Task:", "Format:")]
    assert positions == sorted(positions)

    for marker in (
        "get_weather",
        "add_reminder",
        "карточку подтверждения",
        "без вызова инструментов",
        "обнаруженного каталога",
        "не ограничивай набор инструментов ролью",
        "/study",
    ):
        assert marker in prompt


def test_everyday_is_exposed_in_start_menu_and_settings() -> None:
    keyboard = get_roles_keyboard()
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert callbacks == [f"set_role:{mode.id}" for mode in registry.list_modes()]

    everyday_button = keyboard.inline_keyboard[-1][0]
    assert everyday_button.callback_data == "set_role:everyday"
    assert everyday_button.text == f"{ROLE_ICONS['everyday']} Повседневный помощник"


def test_everyday_is_registered_in_telegram_commands() -> None:
    commands = {command.command: command.description for command in get_default_bot_commands()}
    assert "everyday" in commands
    assert commands["everyday"].startswith("Повседневный помощник:")
    assert len(commands["everyday"]) <= 256


async def test_start_and_settings_show_everyday_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr(command_handlers, "send_formatted_message", send)

    await command_handlers.handle_start(make_message("/start"))
    assert send.await_args is not None
    start_text = send.await_args.args[1]
    start_markup = send.await_args.kwargs["reply_markup"]
    assert "/everyday" in start_text
    assert "set_role:everyday" in [
        button.callback_data for row in start_markup.inline_keyboard for button in row
    ]

    send.reset_mock()
    service = MagicMock()
    service.model_name = "test-model"
    service.get_user_status = AsyncMock(
        return_value=(
            UserSettings(user_id=42, mode="everyday", temperature=0.7),
            EVERYDAY_MODE,
        )
    )

    await command_handlers.handle_settings(make_message("/settings"), service)

    assert send.await_args is not None
    settings_text = send.await_args.args[1]
    settings_markup = send.await_args.kwargs["reply_markup"]
    assert "Повседневный помощник" in settings_text
    assert "/everyday" in settings_text
    assert "set_role:everyday" in [
        button.callback_data for row in settings_markup.inline_keyboard for button in row
    ]


async def test_everyday_command_switches_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MagicMock()
    service.switch_mode = AsyncMock(return_value=EVERYDAY_MODE)
    send = AsyncMock()
    monkeypatch.setattr(command_handlers, "send_formatted_message", send)

    await command_handlers.handle_mode_switch(make_message("/everyday"), service)

    service.switch_mode.assert_awaited_once_with(42, "everyday")
    assert send.await_args is not None
    assert "Повседневный помощник" in send.await_args.args[1]


async def test_everyday_selection_persists_in_existing_user_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {"mode": "study"}

    async def fake_get_or_create_user(pool: object, user_id: int) -> UserSettings:
        return UserSettings(user_id=user_id, mode=stored["mode"])

    async def fake_set_user_mode(pool: object, user_id: int, mode: str) -> None:
        stored["mode"] = mode

    async def fake_clear_history(pool: object, user_id: int) -> None:
        pass

    monkeypatch.setattr(dialog_service_module, "get_or_create_user", fake_get_or_create_user)
    monkeypatch.setattr(dialog_service_module, "set_user_mode", fake_set_user_mode)
    monkeypatch.setattr(dialog_service_module, "clear_dialog_history", fake_clear_history)

    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
    )
    service = DialogService(
        cast(asyncpg.Pool, MagicMock()),
        settings,
        MagicMock(),
        registry,
    )

    mode = await service.switch_mode(42, "everyday")

    assert mode is EVERYDAY_MODE
    assert stored["mode"] == "everyday"

    user, active_mode = await service.get_user_status(42)
    assert user.mode == "everyday"
    assert active_mode.id == "everyday"

    await service.switch_mode(42, "study")
    assert stored["mode"] == "study"


async def test_everyday_plain_question_answers_without_tools_but_exposes_shared_catalog(
    math_tools: list[types.Tool],
) -> None:
    client = EverydayToolClient(math_tools)
    agent, llm, _ = make_agent(client, [LLMResponse(content="Короткий практичный ответ.")])

    result = await agent.run(
        everyday_messages("Как лучше спланировать покупки?"),
        CONTEXT,
        timezone="Europe/Moscow",
    )

    assert result.decision == AgentDecision.RESPOND
    assert result.text == "Короткий практичный ответ."
    client.tool_calls.assert_not_awaited()
    client.resource_reads.assert_not_awaited()

    request = llm.requests[0]
    definitions = {tool.name for tool in request.tools}
    assert set(client.tools) <= definitions
    expected_tools = {"get_weather", "add_reminder", "math_calculate", "math_plot", "math_solve"}
    assert expected_tools <= definitions
    assert READ_RESOURCE_FUNCTION in definitions
    assert "повседневный помощник" in request.messages[0].content.lower()


async def test_everyday_weather_answer_uses_get_weather(
    math_tools: list[types.Tool],
) -> None:
    client = EverydayToolClient(math_tools)
    client.tool_calls.return_value = weather_payload()
    agent, llm, _ = make_agent(
        client,
        [
            tool_response(tool_call("get_weather", {"city": "Казань"})),
            LLMResponse(content="В Казани около 5 °C, возьмите лёгкую куртку."),
        ],
    )

    result = await agent.run(
        everyday_messages("Какая погода в Казани?"),
        CONTEXT,
        timezone="Europe/Moscow",
    )

    assert result.decision == AgentDecision.TOOL
    assert result.tool_name == "get_weather"
    assert result.text == "В Казани около 5 °C, возьмите лёгкую куртку."
    client.tool_calls.assert_awaited_once_with("get_weather", {"city": "Казань"})

    tool_result = next(
        message
        for message in llm.requests[1].messages
        if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"
    )
    envelope = json.loads(tool_result.content)
    assert envelope["source"] == "get_weather"
    assert envelope["data"]["city"] == "Казань"


async def test_everyday_explicit_plot_request_executes_math_plot(
    math_tools: list[types.Tool],
) -> None:
    client = EverydayToolClient(math_tools)
    png = make_png()
    client.tool_calls.return_value = math_plot_payload(png)
    agent, llm, _ = make_agent(
        client,
        [
            tool_response(tool_call("math_plot", {"functions": ["x^2"]})),
            LLMResponse(content="График готов."),
        ],
    )

    result = await agent.run(
        everyday_messages("Построй график x^2."),
        CONTEXT,
        timezone="Europe/Moscow",
    )

    assert result.decision == AgentDecision.TOOL
    assert result.tool_name == "math_plot"
    assert result.text == "График готов."
    assert len(result.images) == 1
    client.tool_calls.assert_awaited_once_with("math_plot", {"functions": ["x^2"]})

    definitions = {tool.name for tool in llm.requests[0].tools}
    assert "math_plot" in definitions

    tool_result = next(
        message
        for message in llm.requests[1].messages
        if message.role == MessageRole.TOOL and message.tool_call_id == "call-1"
    )
    envelope = json.loads(tool_result.content)
    assert envelope["source"] == "math_plot"
    assert envelope["data"]["status"] == "ok"


async def test_everyday_reads_shared_schedule_resource(
    math_tools: list[types.Tool],
) -> None:
    client = EverydayToolClient(math_tools)
    agent, llm, _ = make_agent(
        client,
        [
            tool_response(tool_call(READ_RESOURCE_FUNCTION, {"uri": RESOURCE_URI})),
            LLMResponse(content="Расписание получено."),
        ],
    )

    result = await agent.run(
        everyday_messages("Покажи расписание на эту неделю."),
        CONTEXT,
        timezone="Europe/Moscow",
    )

    assert result.decision == AgentDecision.RESPOND
    assert result.resource_uris == [RESOURCE_URI]
    assert result.text == "Расписание получено."
    client.resource_reads.assert_awaited_once_with(RESOURCE_URI)
    client.tool_calls.assert_not_awaited()

    definitions = {tool.name for tool in llm.requests[0].tools}
    assert READ_RESOURCE_FUNCTION in definitions


async def test_everyday_reminder_prepares_confirmation_without_creating(
    math_tools: list[types.Tool],
) -> None:
    client = EverydayToolClient(math_tools)
    confirmation_id = "3a55a3ae-3f87-4f34-b60b-4f6c6f5e3f5d"
    confirmations = MagicMock()
    confirmations.prepare = AsyncMock(
        return_value=AgentResult(
            text="Проверьте текст и время напоминания.",
            decision=AgentDecision.PREPARE,
            confirmation_id=confirmation_id,
        )
    )
    arguments: dict[str, object] = {
        "text": "позвонить маме",
        "remind_at": "2026-10-13T19:00:00+03:00",
    }
    agent, _, _ = make_agent(
        client,
        [tool_response(tool_call("add_reminder", arguments))],
        confirmations=confirmations,
    )

    result = await agent.run(
        everyday_messages("Напомни завтра в 19:00 позвонить маме."),
        CONTEXT,
        timezone="Europe/Moscow",
    )

    assert result.decision == AgentDecision.PREPARE
    assert result.confirmation_id == confirmation_id
    assert result.reason == "awaiting_confirmation"
    assert result.text == "Проверьте текст и время напоминания."
    client.tool_calls.assert_not_awaited()
    confirmations.prepare.assert_awaited_once_with(
        CONTEXT,
        arguments,
        "Europe/Moscow",
        position=0,
        calls_remaining=5,
    )
