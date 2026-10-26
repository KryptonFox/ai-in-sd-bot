import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User

from app.core.schemas.agent import AgentResult
from app.handlers import agent_commands


class QueueSpy:
    def __init__(self) -> None:
        self.acquisitions: list[tuple[int, bool]] = []
        self.inside = False

    @asynccontextmanager
    async def acquire(self, user_id: int, ignore_limits: bool = False):
        self.acquisitions.append((user_id, ignore_limits))
        self.inside = True
        try:
            yield 0
        finally:
            self.inside = False


def make_message(text: str = "/tools") -> Message:
    return Message(
        message_id=12,
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=100, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        text=text,
    )


def make_service() -> MagicMock:
    service = MagicMock()
    service.queue_manager = QueueSpy()
    service.tools_status = AsyncMock(return_value="Инструменты доступны")
    service.timezone_status = AsyncMock(return_value="Часовой пояс: Europe/Moscow")
    service.why = AsyncMock(return_value="Подготовлено напоминание")
    service.confirm_action = AsyncMock(return_value=AgentResult(text="Готово", decision="respond"))
    return service


@pytest.mark.asyncio
async def test_tools_and_why_are_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service()
    message = make_message("/tools")
    send = AsyncMock()
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_tools(message, service)
    await agent_commands.handle_why(make_message("/why"), service)

    service.tools_status.assert_awaited_once_with()
    service.why.assert_awaited_once_with(42)
    assert service.queue_manager.acquisitions == []
    assert [call.args[1] for call in send.await_args_list] == [
        "Инструменты доступны",
        "Подготовлено напоминание",
    ]


@pytest.mark.asyncio
async def test_timezone_status_and_update_use_expected_queue_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    message = make_message("/timezone")
    send = AsyncMock()
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_timezone(message, service)
    assert service.queue_manager.acquisitions == []
    service.timezone_status.assert_awaited_once_with(42)

    await agent_commands.handle_timezone(make_message("/timezone Europe/Berlin"), service)
    assert service.queue_manager.acquisitions == [(42, True)]
    service.timezone_status.assert_awaited_with(42, "Europe/Berlin")
    assert [call.args[1] for call in send.await_args_list] == [
        "Часовой пояс: Europe/Moscow",
        "Часовой пояс: Europe/Moscow",
    ]


@pytest.mark.asyncio
async def test_timezone_invalid_zone_gets_friendly_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    service.timezone_status = AsyncMock(side_effect=ValueError("bad zone detail"))
    send = AsyncMock()
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_timezone(make_message("/timezone Invalid/Zone"), service)

    response = send.await_args.args[1]
    assert "часовой пояс" in response
    assert "Invalid/Zone" not in response
    assert service.queue_manager.acquisitions == [(42, True)]


def make_callback(data: str, message: object | None = None) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message = message
    callback.from_user = User(id=42, is_bot=False, first_name="Test")
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "cancel"), [("confirm", False), ("cancel", True)])
async def test_confirmation_callback_calls_service_under_queue_and_sends_result(
    action: str,
    cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = str(uuid.uuid4())
    message = make_message()
    service = make_service()
    send = AsyncMock()
    edit_markup = AsyncMock()
    monkeypatch.setattr(Message, "edit_reply_markup", edit_markup)
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_confirmation_callback(
        make_callback(f"{action}:{identifier}", message), service
    )

    assert service.queue_manager.acquisitions == [(42, True)]
    service.confirm_action.assert_awaited_once_with(42, identifier, cancel=cancel)
    edit_markup.assert_awaited_once_with(reply_markup=None)
    assert send.await_args.args == (message, "Готово")


@pytest.mark.asyncio
async def test_error_result_preserves_confirmation_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid.uuid4())
    message = make_message()
    service = make_service()
    service.confirm_action = AsyncMock(
        return_value=AgentResult(text="Действие недоступно", decision="error")
    )
    send = AsyncMock()
    edit_markup = AsyncMock()
    monkeypatch.setattr(Message, "edit_reply_markup", edit_markup)
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_confirmation_callback(
        make_callback(f"confirm:{identifier}", message), service
    )

    edit_markup.assert_not_awaited()
    assert send.await_args.args == (message, "Действие недоступно")


@pytest.mark.asyncio
async def test_confirmation_ignores_markup_removal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid.uuid4())
    message = make_message()
    service = make_service()
    send = AsyncMock()
    edit_markup = AsyncMock(side_effect=RuntimeError("private Telegram detail"))
    monkeypatch.setattr(Message, "edit_reply_markup", edit_markup)
    monkeypatch.setattr(agent_commands, "send_formatted_message", send)

    await agent_commands.handle_confirmation_callback(
        make_callback(f"confirm:{identifier}", message), service
    )

    edit_markup.assert_awaited_once_with(reply_markup=None)
    assert send.await_args.args == (message, "Готово")


@pytest.mark.asyncio
async def test_confirmation_callback_answers_and_rejects_non_message_or_bad_id() -> None:
    service = make_service()
    no_message = make_callback(f"confirm:{uuid.uuid4()}", object())
    await agent_commands.handle_confirmation_callback(no_message, service)
    no_message.answer.assert_awaited_once()
    service.confirm_action.assert_not_awaited()

    bad_id = make_callback("confirm:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", make_message())
    await agent_commands.handle_confirmation_callback(bad_id, service)
    bad_id.answer.assert_awaited_once()
    service.confirm_action.assert_not_awaited()


def test_confirmation_keyboard_callback_data_fits_telegram_limit() -> None:
    identifier = str(uuid.uuid4())
    keyboard = agent_commands.get_confirmation_keyboard(identifier)
    callback_values = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callback_values == [f"confirm:{identifier}", f"cancel:{identifier}"]
    assert all(len(value) < 64 for value in callback_values)
