import logging
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.core.formatters import send_formatted_message
from app.db.notification_records import CityRecord
from app.services.notifications.contracts import (
    CitySelection,
    NotificationFailure,
    NotificationSettingsView,
)
from app.services.notifications.service import NotificationService

logger = logging.getLogger("app.handlers.notifications")
router = Router(name="notifications")
CITY_PICK_PREFIX = "notify_city_pick"
CITY_CANCEL_PREFIX = "notify_city_cancel"


class CityAction(StrEnum):
    PICK = CITY_PICK_PREFIX
    CANCEL = CITY_CANCEL_PREFIX


class NotificationChatMiddleware(BaseMiddleware):
    def __init__(self, service: NotificationService) -> None:
        self._service = service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.chat.type == "private"
            and event.from_user is not None
        ):
            try:
                await self._service.remember_private_chat(
                    event.from_user.id,
                    event.chat.id,
                    event.chat.type,
                )
            except Exception:
                logger.warning("notification_chat_capture_failed")

        return await handler(event, data)


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user is not None else message.chat.id


async def _remember_chat(message: Message, service: NotificationService) -> None:
    if message.chat.type != "private" or message.from_user is None:
        return

    await service.remember_private_chat(
        message.from_user.id,
        message.chat.id,
        message.chat.type,
    )


def _city_buttons(selection: CitySelection) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for index, city in enumerate(selection.candidates):
        label = f"✅ {city.name}, {city.country}"[:64]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CITY_PICK_PREFIX}:{selection.id}:{index}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"{CITY_CANCEL_PREFIX}:{selection.id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _city_line(city: CityRecord) -> str:
    parts = [city.name, city.country]
    if city.region and city.region.casefold() != city.name.casefold():
        parts.insert(1, city.region)
    return ", ".join(parts)


def _cities_text(selection: CitySelection) -> str:
    lines = [
        "🌍 **Выберите населённый пункт:**",
        "",
    ]

    for index, city in enumerate(selection.candidates, start=1):
        lines.append(f"{index}. {_city_line(city)}")

    lines.extend(
        [
            "",
            "Нажмите кнопку ниже, чтобы подтвердить выбор и сохранить координаты.",
        ]
    )
    return "\n".join(lines)


def _settings_text(settings: NotificationSettingsView) -> str:
    city = _city_line(settings.city) if settings.city is not None else "не выбран"
    timezone = settings.timezone or "не задан"
    briefing = "включена (08:00–09:00)" if settings.briefing_enabled else "выключена"
    destination = "сохранён" if settings.destination_saved else "не сохранён"

    return "\n".join(
        [
            "🔔 **Настройки уведомлений**",
            "",
            f"• **Адрес доставки:** {destination}",
            f"• **Город:** {city}",
            f"• **Часовой пояс:** {timezone}",
            f"• **Ежедневная сводка:** {briefing}",
            "",
            "Изменить город: `/city <город, страна>`.",
            "Включить или отключить сводку: `/briefing_on` и `/briefing_off`.",
        ]
    )


def _city_saved_text(settings: NotificationSettingsView) -> str:
    city = _city_line(settings.city) if settings.city is not None else "не выбран"
    timezone = settings.timezone or "не задан"
    return "\n".join(
        [
            f"✅ Город сохранён: {city}.",
            f"Часовой пояс: {timezone}.",
            "",
            "Теперь можно включить ежедневную сводку командой `/briefing_on`.",
        ]
    )


@router.message(Command("city"), F.chat.type == "private")
async def handle_city(message: Message, notification_service: NotificationService) -> None:
    await _remember_chat(message, notification_service)

    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) == 2 else ""
    if not query:
        await send_formatted_message(
            message,
            "Укажите город и страну, например: `/city Казань, Россия`.",
        )
        return

    try:
        selection = await notification_service.prepare_city_selection(
            _user_id(message),
            query,
        )
    except NotificationFailure as error:
        await send_formatted_message(message, error.message)
        return
    except Exception:
        logger.warning("notification_city_prepare_failed")
        await send_formatted_message(
            message,
            "Не удалось найти город. Попробуйте позже.",
        )
        return

    await send_formatted_message(
        message,
        _cities_text(selection),
        reply_markup=_city_buttons(selection),
    )


@router.message(Command("briefing_on"), F.chat.type == "private")
async def handle_briefing_on(
    message: Message,
    notification_service: NotificationService,
) -> None:
    await _remember_chat(message, notification_service)

    try:
        settings = await notification_service.enable_briefing(_user_id(message))
    except NotificationFailure as error:
        await send_formatted_message(message, error.message)
        return
    except Exception:
        logger.warning("notification_briefing_enable_failed")
        await send_formatted_message(
            message,
            "Не удалось включить ежедневную сводку. Попробуйте позже.",
        )
        return

    city = _city_line(settings.city) if settings.city is not None else "не выбран"
    timezone = settings.timezone or "не задан"
    await send_formatted_message(
        message,
        "\n".join(
            [
                "✅ **Ежедневная сводка включена.**",
                f"Город: {city}.",
                f"Часовой пояс: {timezone}.",
                "Сводка придёт между 08:00 и 09:00 по местному времени.",
            ]
        ),
    )


@router.message(Command("briefing_off"), F.chat.type == "private")
async def handle_briefing_off(
    message: Message,
    notification_service: NotificationService,
) -> None:
    await _remember_chat(message, notification_service)

    try:
        await notification_service.disable_briefing(_user_id(message))
    except Exception:
        logger.warning("notification_briefing_disable_failed")
        await send_formatted_message(
            message,
            "Не удалось отключить ежедневную сводку. Попробуйте позже.",
        )
        return

    await send_formatted_message(message, "🔕 **Ежедневная сводка выключена.**")


@router.message(Command("notifications"), F.chat.type == "private")
async def handle_notifications(
    message: Message,
    notification_service: NotificationService,
) -> None:
    await _remember_chat(message, notification_service)

    try:
        settings = await notification_service.notification_settings(_user_id(message))
    except Exception:
        logger.warning("notification_settings_failed")
        await send_formatted_message(
            message,
            "Не удалось прочитать настройки уведомлений. Попробуйте позже.",
        )
        return

    await send_formatted_message(message, _settings_text(settings))


@router.callback_query(
    F.data.startswith(f"{CITY_PICK_PREFIX}:") | F.data.startswith(f"{CITY_CANCEL_PREFIX}:")
)
async def handle_city_callback(
    callback: CallbackQuery,
    notification_service: NotificationService,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Выбор города уже недоступен.", show_alert=True)
        return

    data = callback.data or ""
    action_text, separator, payload = data.partition(":")
    if not separator:
        await callback.answer("Выбор города недоступен.", show_alert=True)
        return

    try:
        action = CityAction(action_text)
    except ValueError:
        await callback.answer("Выбор города недоступен.", show_alert=True)
        return

    if action is CityAction.PICK:
        identifier, index_text = _split_pick_payload(payload)
        index = _parse_index(index_text)
        if identifier is None or index is None:
            await callback.answer("Выбор города недоступен.", show_alert=True)
            return

        try:
            settings = await notification_service.confirm_city_selection(
                callback.from_user.id,
                identifier,
                index,
            )
        except NotificationFailure as error:
            await callback.answer(error.message, show_alert=True)
            return
        except Exception:
            logger.warning("notification_city_confirm_failed")
            await callback.answer("Не удалось сохранить город.", show_alert=True)
            return

        await callback.answer("Город сохранён")
        await _remove_markup(callback)
        await send_formatted_message(message, _city_saved_text(settings))
        return

    identifier = _parse_identifier(payload)
    if identifier is None:
        await callback.answer("Выбор города недоступен.", show_alert=True)
        return

    try:
        await notification_service.cancel_city_selection(
            callback.from_user.id,
            identifier,
        )
    except NotificationFailure as error:
        await callback.answer(error.message, show_alert=True)
        return
    except Exception:
        logger.warning("notification_city_cancel_failed")
        await callback.answer("Не удалось отменить выбор.", show_alert=True)
        return

    await callback.answer("Выбор отменён")
    await _remove_markup(callback)
    await send_formatted_message(message, "Выбор города отменён.")


def _split_pick_payload(payload: str) -> tuple[uuid.UUID | None, str | None]:
    identifier_text, separator, index_text = payload.partition(":")
    if not separator:
        return None, None
    return _parse_identifier(identifier_text), index_text


def _parse_identifier(value: str) -> uuid.UUID | None:
    try:
        identifier = uuid.UUID(value)
    except ValueError:
        return None
    if str(identifier) != value:
        return None
    return identifier


def _parse_index(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    index = int(value)
    return index if index >= 0 else None


async def _remove_markup(callback: CallbackQuery) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("notification_city_markup_unavailable")
