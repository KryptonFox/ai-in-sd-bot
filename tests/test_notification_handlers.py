import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, InlineKeyboardMarkup, Message, User

from app.db.notification_records import CityRecord
from app.handlers import notifications
from app.services.notifications.contracts import (
    CitySelection,
    NotificationFailure,
    NotificationSettingsView,
)

CITY = CityRecord(
    name="Казань",
    country="Россия",
    region="Татарстан",
    latitude=55.7963,
    longitude=49.1088,
)


def make_message(text: str) -> Message:
    return Message(
        message_id=12,
        date=datetime(2031, 5, 1, tzinfo=UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        text=text,
    )


def sent_call(send: AsyncMock):
    call = send.await_args
    assert call is not None
    return call


def make_service() -> MagicMock:
    service = MagicMock()
    service.remember_private_chat = AsyncMock(return_value=True)
    service.prepare_city_selection = AsyncMock(
        return_value=CitySelection(
            id=uuid.uuid4(),
            candidates=(CITY,),
            expires_at=datetime(2031, 5, 1, 12, 5, tzinfo=UTC),
        )
    )
    service.confirm_city_selection = AsyncMock(
        return_value=NotificationSettingsView(
            destination_saved=True,
            timezone="Europe/Moscow",
            city=CITY,
            briefing_enabled=False,
        )
    )
    service.cancel_city_selection = AsyncMock(return_value=None)
    service.enable_briefing = AsyncMock(
        return_value=NotificationSettingsView(
            destination_saved=True,
            timezone="Europe/Moscow",
            city=CITY,
            briefing_enabled=True,
        )
    )
    service.disable_briefing = AsyncMock(
        return_value=NotificationSettingsView(
            destination_saved=True,
            timezone="Europe/Moscow",
            city=CITY,
            briefing_enabled=False,
        )
    )
    service.notification_settings = AsyncMock(
        return_value=NotificationSettingsView(
            destination_saved=True,
            timezone="Europe/Moscow",
            city=CITY,
            briefing_enabled=True,
        )
    )
    return service


def make_callback(data: str, message: Message | None = None) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message = message
    callback.from_user = User(id=42, is_bot=False, first_name="Test")
    callback.answer = AsyncMock()
    return callback


async def test_city_command_shows_confirmation_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_city(make_message("/city Казань, Россия"), service)

    service.remember_private_chat.assert_awaited_once_with(42, 42, "private")
    service.prepare_city_selection.assert_awaited_once_with(42, "Казань, Россия")
    call = sent_call(send)
    text = call.args[1]
    keyboard = call.kwargs["reply_markup"]
    assert "Казань" in text
    assert "Россия" in text
    assert "Татарстан" in text
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert keyboard.inline_keyboard[0][0].callback_data == (
        f"notify_city_pick:{service.prepare_city_selection.return_value.id}:0"
    )


async def test_city_command_without_argument_explains_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_city(make_message("/city"), service)

    service.prepare_city_selection.assert_not_awaited()
    assert "/city" in sent_call(send).args[1]


async def test_city_callback_confirms_owner_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    selection = service.prepare_city_selection.return_value
    message = make_message("/city Казань")
    callback = make_callback(f"notify_city_pick:{selection.id}:0", message)
    send = AsyncMock()
    edit_markup = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_markup)

    await notifications.handle_city_callback(callback, service)

    service.confirm_city_selection.assert_awaited_once_with(42, selection.id, 0)
    callback.answer.assert_awaited_once_with("Город сохранён")
    edit_markup.assert_awaited_once_with(reply_markup=None)
    assert sent_call(send).args == (
        message,
        "✅ Город сохранён: Казань, Татарстан, Россия.\n"
        "Часовой пояс: Europe/Moscow.\n\n"
        "Теперь можно включить ежедневную сводку командой `/briefing_on`.",
    )


async def test_city_callback_rejects_tampered_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    callback = make_callback(
        "notify_city_pick:not-a-uuid:0",
        make_message("/city Казань"),
    )
    monkeypatch.setattr(notifications, "send_formatted_message", AsyncMock())

    await notifications.handle_city_callback(callback, service)

    service.confirm_city_selection.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_city_callback_cancel_is_owner_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    selection_id = uuid.uuid4()
    callback = make_callback(
        f"notify_city_cancel:{selection_id}",
        make_message("/city Казань"),
    )
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)
    monkeypatch.setattr(Message, "edit_reply_markup", AsyncMock())

    await notifications.handle_city_callback(callback, service)

    service.cancel_city_selection.assert_awaited_once_with(42, selection_id)
    assert sent_call(send).args[1] == "Выбор города отменён."


async def test_briefing_on_reports_missing_city(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    service.enable_briefing = AsyncMock(
        side_effect=NotificationFailure(
            "city_required",
            "Сначала выберите город командой /city <город, страна> и подтвердите выбор.",
        )
    )
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_briefing_on(make_message("/briefing_on"), service)

    assert "выберите город" in sent_call(send).args[1]


async def test_briefing_on_success_includes_city_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_briefing_on(make_message("/briefing_on"), service)

    text = sent_call(send).args[1]
    assert "Ежедневная сводка включена" in text
    assert "08:00" in text
    assert "09:00" in text


async def test_briefing_off_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_briefing_off(make_message("/briefing_off"), service)

    service.disable_briefing.assert_awaited_once_with(42)
    assert "выключена" in sent_call(send).args[1]


async def test_notifications_shows_settings_without_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    send = AsyncMock()
    monkeypatch.setattr(notifications, "send_formatted_message", send)

    await notifications.handle_notifications(make_message("/notifications"), service)

    text = sent_call(send).args[1]
    assert "Казань" in text
    assert "Europe/Moscow" in text
    assert "включена" in text
    assert "**Адрес доставки:** сохранён" in text
    assert "42" not in text


async def test_middleware_captures_only_private_messages() -> None:
    service = make_service()
    middleware = notifications.NotificationChatMiddleware(service)
    handler = AsyncMock(return_value="handled")
    data: dict[str, object] = {}

    result = await middleware(handler, make_message("привет"), data)

    assert result == "handled"
    service.remember_private_chat.assert_awaited_once_with(42, 42, "private")

    service.remember_private_chat.reset_mock()
    group_message = Message(
        message_id=13,
        date=datetime(2031, 5, 1, tzinfo=UTC),
        chat=Chat(id=-100, type="group"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        text="привет",
    )

    await middleware(handler, group_message, data)

    service.remember_private_chat.assert_not_awaited()


async def test_middleware_does_not_block_on_capture_failure() -> None:
    service = make_service()
    service.remember_private_chat = AsyncMock(side_effect=RuntimeError("db down"))
    middleware = notifications.NotificationChatMiddleware(service)
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, make_message("привет"), {})

    assert result == "handled"
