import logging
import uuid
from zoneinfo import ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.formatters import send_formatted_message
from app.core.schemas.agent import AgentResult
from app.services.dialog_service import DialogService

logger = logging.getLogger("app.handlers.agent_commands")
router = Router(name="agent_commands")


def get_confirmation_keyboard(identifier: str) -> InlineKeyboardMarkup:
    parsed = uuid.UUID(identifier)
    normalized = str(parsed)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm:{normalized}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{normalized}"),
            ]
        ]
    )


@router.message(Command("tools"), F.chat.type == "private")
async def handle_tools(message: Message, dialog_service: DialogService) -> None:
    status = await dialog_service.tools_status()
    await send_formatted_message(message, status)


@router.message(Command("timezone"), F.chat.type == "private")
async def handle_timezone(message: Message, dialog_service: DialogService) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    parts = (message.text or "").split(maxsplit=1)
    zone = parts[1].strip() if len(parts) == 2 else None
    try:
        if zone:
            async with dialog_service.queue_manager.acquire(user_id, ignore_limits=True):
                status = await dialog_service.timezone_status(user_id, zone)
        else:
            status = await dialog_service.timezone_status(user_id)
    except (ValueError, ZoneInfoNotFoundError):
        status = (
            "❌ Не удалось распознать часовой пояс. Укажите зону IANA, например "
            "`/timezone Europe/Moscow`."
        )
    await send_formatted_message(message, status)


@router.message(Command("why"), F.chat.type == "private")
async def handle_why(message: Message, dialog_service: DialogService) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    explanation = await dialog_service.why(user_id)
    await send_formatted_message(message, explanation)


@router.callback_query(F.data.startswith("confirm:") | F.data.startswith("cancel:"))
async def handle_confirmation_callback(
    callback: CallbackQuery, dialog_service: DialogService
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Это действие больше недоступно.", show_alert=True)
        return
    data = callback.data or ""
    action, separator, identifier = data.partition(":")
    try:
        valid_identifier = len(identifier) == 36 and str(uuid.UUID(identifier)) == identifier
    except ValueError:
        valid_identifier = False
    if not separator or action not in {"confirm", "cancel"} or not valid_identifier:
        await callback.answer("Идентификатор действия недействителен.", show_alert=True)
        return

    await callback.answer()
    user_id = callback.from_user.id
    async with dialog_service.queue_manager.acquire(user_id, ignore_limits=True):
        result: AgentResult = await dialog_service.confirm_action(
            user_id, identifier, cancel=action == "cancel"
        )
    if result.decision != "error":
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Не удалось обновить клавиатуру подтверждения")
    await send_formatted_message(message, result.text)
