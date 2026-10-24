import asyncio
import logging
import uuid
from contextlib import nullcontext

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.context import generate_request_id, set_request_id
from app.core.exceptions import (
    LLMEmptyResponseError,
    LLMServiceError,
    LLMTimeoutError,
    QueueFullError,
)
from app.core.formatters import send_formatted_message
from app.services.dialog_service import DialogService

logger = logging.getLogger("app.handlers.dialog")
router = Router(name="dialog")
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TYPING_INTERVAL = 4.0
_retry_cache: dict[str, tuple[int, str]] = {}


def split_text(text: str, max_chunk_size: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= max_chunk_size:
        return [text]
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0
    paragraphs = text.split("\n")
    for para in paragraphs:
        if len(para) > max_chunk_size:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            start = 0
            while start < len(para):
                end = start + max_chunk_size
                chunks.append(para[start:end])
                start = end
            continue
        para_len = len(para) + (1 if current_chunk else 0)
        if current_length + para_len > max_chunk_size:
            chunks.append("\n".join(current_chunk))
            current_chunk = [para]
            current_length = len(para)
        else:
            current_chunk.append(para)
            current_length += para_len
    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return [c for c in chunks if c]


def get_retry_keyboard(user_id: int, text: str) -> InlineKeyboardMarkup:
    retry_id = uuid.uuid4().hex[:12]
    _retry_cache[retry_id] = (user_id, text)
    if len(_retry_cache) > 1000:
        _retry_cache.pop(next(iter(_retry_cache)))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"retry:{retry_id}")]
        ]
    )


def find_retry_text(retry_id: str, user_id: int, message: Message | None) -> str | None:
    cached = _retry_cache.get(retry_id)
    if cached and cached[0] == user_id:
        return cached[1]
    if message and message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text
    return None


async def _send_typing(message: Message) -> None:
    if not message.bot:
        return

    while True:
        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id,
                action=ChatAction.TYPING,
                request_timeout=TYPING_INTERVAL,
            )
        except Exception:
            logger.debug("Не удалось отправить статус typing")
        await asyncio.sleep(TYPING_INTERVAL)


async def _execute_dialog(
    target_message: Message,
    dialog_service: DialogService,
    user_id: int,
    user_text: str,
) -> None:
    req_id = generate_request_id()
    set_request_id(req_id)

    queue_manager = getattr(dialog_service, "queue_manager", None)
    queue_msg = None

    if queue_manager and await queue_manager.is_user_busy(user_id):
        try:
            queue_msg = await target_message.answer(
                "⏳ Ваше сообщение поставлено в очередь. Дождитесь ответа на предыдущий запрос..."
            )
        except Exception:
            pass

    try:
        ctx = queue_manager.acquire(user_id) if queue_manager else nullcontext()
        async with ctx:
            if queue_msg and hasattr(queue_msg, "delete") and callable(queue_msg.delete):
                try:
                    await queue_msg.delete()
                except Exception:
                    pass

            typing_task = asyncio.create_task(_send_typing(target_message))
            try:
                response_text = await dialog_service.process_user_message(user_id, user_text)
            finally:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            chunks = split_text(response_text)
            for chunk in chunks:
                await send_formatted_message(target_message, chunk)

    except QueueFullError:
        logger.warning("[%s] Очередь пользователя %d переполнена", req_id, user_id)
        if queue_msg and hasattr(queue_msg, "delete") and callable(queue_msg.delete):
            try:
                await queue_msg.delete()
            except Exception:
                pass
        await send_formatted_message(
            target_message,
            "⏳ В очереди уже находится максимальное количество ваших сообщений. "
            "Пожалуйста, дождитесь ответа бота перед отправкой новых запросов.",
        )

    except LLMTimeoutError:
        logger.warning("[%s] Истёк таймаут ответа LLM для пользователя %d", req_id, user_id)
        keyboard = get_retry_keyboard(user_id, user_text)
        await send_formatted_message(
            target_message,
            "⏳ Превышено время ожидания ответа от языковой модели. Пожалуйста, попробуйте позже.",
            reply_markup=keyboard,
            as_reply=True,
        )

    except LLMEmptyResponseError:
        logger.warning("[%s] Получен пустой ответ от LLM для пользователя %d", req_id, user_id)
        await send_formatted_message(
            target_message,
            "⚠️ Языковая модель вернула пустой ответ. Попробуйте переформулировать запрос.",
        )

    except LLMServiceError:
        logger.error("[%s] Ошибка сервиса LLM для пользователя %d", req_id, user_id)
        await send_formatted_message(
            target_message,
            "⚠️ Сервис языковой модели временно недоступен. Попробуйте повторить запрос позже.",
        )

    except Exception:
        logger.exception(
            "[%s] Непредвиденная ошибка при обработке сообщения пользователя %d", req_id, user_id
        )
        await send_formatted_message(
            target_message,
            "❌ Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.",
        )


@router.message(F.text, F.chat.type == "private", ~F.text.startswith("/"))
async def handle_user_text(message: Message, dialog_service: DialogService) -> None:
    if not message.text:
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    await _execute_dialog(message, dialog_service, user_id, message.text)


@router.callback_query(F.data.startswith("retry:"))
async def handle_retry_callback(callback: CallbackQuery, dialog_service: DialogService) -> None:
    if not callback.data or not callback.message:
        return

    retry_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    target_msg = callback.message if isinstance(callback.message, Message) else None
    user_text = find_retry_text(retry_id, user_id, target_msg)

    if not user_text:
        await callback.answer(
            "Не удалось найти исходный запрос. Пожалуйста, отправьте его заново.", show_alert=True
        )
        return

    await callback.answer("Повторяю запрос...")

    if target_msg:
        try:
            await target_msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        run_target = target_msg.reply_to_message or target_msg
    else:
        run_target = target_msg

    if run_target:
        await _execute_dialog(run_target, dialog_service, user_id, user_text)
