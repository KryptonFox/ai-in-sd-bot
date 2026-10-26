import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.exceptions import InvalidTemperatureError
from app.core.formatters import send_formatted_message
from app.modes.registry import registry
from app.services.dialog_service import DialogService

logger = logging.getLogger("app.handlers.commands")
router = Router(name="commands")

ROLE_ICONS = {"study": "🎓", "translate": "🌐", "review": "🔍"}


def get_roles_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{ROLE_ICONS.get(mode.id, '🤖')} {mode.name}",
                callback_data=f"set_role:{mode.id}",
            )
        ]
        for mode in registry.list_modes()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("start", "help", "menu"), F.chat.type == "private")
async def handle_start(message: Message) -> None:
    text = (
        "👋 Привет! Я твой учебный AI-ассистент.\n\n"
        f"{registry.format_help()}\n\n"
        "Выбери нужную роль с помощью кнопок ниже или отправь команду из списка.\n\n"
        "⚙️ **Управление настройками:**\n"
        "• `/settings` — текущий режим, модель и настройка температуры генерации\n"
        "• `/reset` — очистить историю текущего диалога\n"
        "• `/tools` — обнаруженные возможности MCP\n"
        "• `/timezone Europe/Moscow` — часовой пояс для напоминаний\n"
        "• `/why` — последнее решение агента\n\n"
        "Отправь мне текстовое сообщение, и я отвечу в рамках активного режима!"
    )
    await send_formatted_message(message, text, reply_markup=get_roles_keyboard())


@router.message(Command(*registry.commands()), F.chat.type == "private")
async def handle_mode_switch(message: Message, dialog_service: DialogService) -> None:
    if not message.text:
        return

    command = message.text.strip().lstrip("/").split()[0].lower()
    user_id = message.from_user.id if message.from_user else message.chat.id
    mode = await dialog_service.switch_mode(user_id, command)
    logger.info("mode_changed mode=%s", mode.id)

    text = (
        f"✅ Режим переключен на: **{mode.name}**\n\n"
        f"_{mode.description}_\n\n"
        "Контекст предыдущего диалога очищен. Можешь задавать вопросы!"
    )
    await send_formatted_message(message, text)


@router.callback_query(F.data.startswith("set_role:") | F.data.startswith("set_mode:"))
async def handle_role_callback(callback: CallbackQuery, dialog_service: DialogService) -> None:
    if not callback.data or not isinstance(callback.message, Message):
        return

    role_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    mode = await dialog_service.switch_mode(user_id, role_id)
    logger.info("mode_changed mode=%s", mode.id)

    await callback.answer(f"Выбрана роль: {mode.name}")
    text = (
        f"✅ Режим переключен на: **{mode.name}**\n\n"
        f"_{mode.description}_\n\n"
        "Контекст предыдущего диалога очищен. Можешь задавать вопросы!"
    )
    await send_formatted_message(callback.message, text)


@router.message(Command("reset"), F.chat.type == "private")
async def handle_reset(message: Message, dialog_service: DialogService) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    await dialog_service.reset_history(user_id)
    logger.info("history_reset")

    await send_formatted_message(
        message, "🧹 История диалога очищена. Текущий режим и температура сохранены."
    )


@router.message(Command("settings"), F.chat.type == "private")
async def handle_settings(message: Message, dialog_service: DialogService) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    args = message.text.split()[1:] if message.text else []

    if args:
        val_str = args[0].replace(",", ".")
        try:
            temp_val = float(val_str)
            updated_temp = await dialog_service.set_temperature(user_id, temp_val)
            await send_formatted_message(
                message, f"✅ Температура генерации успешно установлена на `{updated_temp}`."
            )
            return
        except (ValueError, InvalidTemperatureError):
            await send_formatted_message(
                message,
                "❌ Некорректное значение температуры. Допустимы только: 0.0, 0.3, 0.7, 1.0.\n"
                "Пример: `/settings 0.7`",
            )
            return

    user, mode = await dialog_service.get_user_status(user_id)
    text = (
        "⚙️ **Текущие настройки ассистента:**\n\n"
        f"• **Активный режим:** {mode.name} (`/{mode.command}`)\n"
        f"• **Модель:** `{dialog_service._settings.llm_model}`\n"
        f"• **Температура генерации:** `{user.temperature}`\n\n"
        "Для изменения температуры отправьте команду с желаемым значением:\n"
        "• `/settings 0.0` — максимальная строгость и детерминизм\n"
        "• `/settings 0.3` — сфокусированные и точные ответы\n"
        "• `/settings 0.7` — сбалансированный режим (по умолчанию)\n"
        "• `/settings 1.0` — повышенная вариативность и креативность"
    )

    role_buttons = [
        InlineKeyboardButton(
            text=f"{ROLE_ICONS.get(m.id, '🤖')} {m.name}", callback_data=f"set_role:{m.id}"
        )
        for m in registry.list_modes()
    ]
    temp_buttons = [
        InlineKeyboardButton(text="0.0", callback_data="set_temp:0.0"),
        InlineKeyboardButton(text="0.3", callback_data="set_temp:0.3"),
        InlineKeyboardButton(text="0.7", callback_data="set_temp:0.7"),
        InlineKeyboardButton(text="1.0", callback_data="set_temp:1.0"),
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[role_buttons, temp_buttons])
    await send_formatted_message(message, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("set_temp:"))
async def handle_temp_callback(callback: CallbackQuery, dialog_service: DialogService) -> None:
    if not callback.data or not isinstance(callback.message, Message):
        return

    val_str = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    try:
        temp_val = float(val_str)
        updated_temp = await dialog_service.set_temperature(user_id, temp_val)
        await callback.answer(f"Температура изменена на {updated_temp}")
        await send_formatted_message(
            callback.message,
            f"✅ Температура генерации успешно установлена на `{updated_temp}`.",
        )
    except (ValueError, InvalidTemperatureError):
        await callback.answer("Ошибка: недопустимое значение", show_alert=True)
