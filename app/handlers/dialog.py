import asyncio
import logging
import math
import uuid

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.context import generate_request_id, set_request_id
from app.core.exceptions import (
    InterruptedRunError,
    LLMEmptyResponseError,
    LLMRefusalError,
    LLMServiceError,
    LLMTimeoutError,
    QueueFullError,
)
from app.core.formatters import send_formatted_message
from app.core.schemas.agent import AgentResult
from app.handlers.agent_commands import get_confirmation_keyboard
from app.llm.diagnostics import generation_error_details
from app.presentation.agent import reply_text
from app.services.dialog_service import DialogService

logger = logging.getLogger("app.handlers.dialog")
router = Router(name="dialog")
TYPING_INTERVAL = 4.0
_retry_cache: dict[str, tuple[int, str]] = {}


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
                request_timeout=math.ceil(TYPING_INTERVAL),
            )
        except Exception:
            logger.debug("Не удалось отправить статус typing")
        await asyncio.sleep(TYPING_INTERVAL)


async def _execute_dialog(
    target_message: Message,
    dialog_service: DialogService,
    user_id: int,
    user_text: str,
    event_id: str | None = None,
) -> None:
    req_id = generate_request_id()
    set_request_id(req_id)

    queue_msg = None
    if await dialog_service.is_busy(user_id):
        try:
            queue_msg = await target_message.answer(
                "⏳ Ваше сообщение поставлено в очередь. Дождитесь ответа на предыдущий запрос..."
            )
        except Exception:
            pass

    try:
        typing_task = asyncio.create_task(_send_typing(target_message))
        try:
            if event_id is None:
                result = await dialog_service.process_user_message(user_id, user_text)
            else:
                result = await dialog_service.process_user_message(
                    user_id, user_text, event_id=event_id
                )
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
            if queue_msg:
                try:
                    await queue_msg.delete()
                except Exception:
                    pass
        keyboard = (
            get_confirmation_keyboard(result.confirmation_id)
            if isinstance(result, AgentResult) and result.confirmation_id
            else None
        )
        await send_formatted_message(target_message, reply_text(result), reply_markup=keyboard)

    except QueueFullError:
        logger.warning("[%s] user_queue_full", req_id)
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

    except InterruptedRunError:
        await send_formatted_message(
            target_message,
            "Запрос прерван после обращения к инструменту. Отправьте новый запрос. "
            "Состояние подтверждённых действий сверяется отдельно.",
        )

    except LLMTimeoutError:
        logger.warning("[%s] llm_timeout", req_id)
        keyboard = get_retry_keyboard(user_id, user_text)
        await send_formatted_message(
            target_message,
            "⏳ Превышено время ожидания ответа от языковой модели. Пожалуйста, попробуйте позже.",
            reply_markup=keyboard,
            as_reply=True,
        )

    except LLMEmptyResponseError:
        logger.warning("[%s] llm_empty", req_id)
        await send_formatted_message(
            target_message,
            "⚠️ Языковая модель вернула пустой ответ. Попробуйте переформулировать запрос.",
        )

    except LLMRefusalError:
        await send_formatted_message(
            target_message,
            "Модель отказалась выполнять запрос. Попробуйте изменить задачу или её формулировку.",
        )

    except LLMServiceError as error:
        logger.error(
            "[%s] llm_error code=%s status=%s reason=%s details=%s",
            req_id,
            error.code,
            error.status_code,
            error.user_message,
            error.diagnostic_details or generation_error_details(error),
        )
        await send_formatted_message(
            target_message,
            f"⚠️ {error.user_message}",
        )

    except Exception as error:
        logger.error("[%s] dialog_error exception=%s", req_id, type(error).__name__)
        await send_formatted_message(
            target_message,
            "❌ Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.",
        )


@router.message(F.text, F.chat.type == "private", ~F.text.startswith("/"))
async def handle_user_text(message: Message, dialog_service: DialogService) -> None:
    if not message.text:
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    await _execute_dialog(
        message,
        dialog_service,
        user_id,
        message.text,
        event_id=f"telegram:{message.chat.id}:{message.message_id}",
    )


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
        await _execute_dialog(
            run_target,
            dialog_service,
            user_id,
            user_text,
            event_id=f"telegram:{run_target.chat.id}:{run_target.message_id}",
        )
