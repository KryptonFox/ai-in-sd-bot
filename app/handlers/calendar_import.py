import io
import logging
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.types import Message

from app.core.clock import SystemClock
from app.core.formatters import send_formatted_message
from app.core.schemas.calendar import (
    MAX_CALENDAR_BYTES,
    CalendarImportError,
    CalendarImportSummary,
)
from app.services.calendar_import import CalendarImportService, is_ics_filename

logger = logging.getLogger("app.handlers.calendar_import")
router = Router(name="calendar_import")
FILE_TOO_LARGE_MESSAGE = (
    f"❌ Файл календаря больше {MAX_CALENDAR_BYTES // 1024} КБ. "
    "Экспортируйте расписание меньшего размера."
)
DOWNLOAD_FAILED_MESSAGE = "❌ Не удалось загрузить файл. Пришлите его ещё раз."
IMPORT_FAILED_MESSAGE = "❌ Не удалось импортировать расписание. Прежнее расписание сохранено."


class CalendarFileTooLarge(Exception):
    pass


class BoundedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self._limit:
            raise CalendarFileTooLarge

        return super().write(data)


class ICalendarDocumentFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.document is not None and is_ics_filename(message.document.file_name)


@router.message(F.chat.type == "private", F.document, ICalendarDocumentFilter())
async def handle_calendar_document(
    message: Message,
    db: asyncpg.Pool,
    calendar_import_service: CalendarImportService | None = None,
) -> None:
    sender = message.from_user
    if sender is None or message.chat.type != "private" or message.chat.id != sender.id:
        await send_formatted_message(
            message,
            "❌ Импорт расписания доступен только владельцу в личном чате.",
        )
        return

    document = message.document
    if document is None:
        return

    if document.file_size is not None and document.file_size > MAX_CALENDAR_BYTES:
        await send_formatted_message(message, FILE_TOO_LARGE_MESSAGE)
        return

    buffer = BoundedBytesIO(MAX_CALENDAR_BYTES)
    try:
        if message.bot is None:
            raise RuntimeError("telegram bot is unavailable")
        await message.bot.download(document, destination=buffer)
    except CalendarFileTooLarge:
        await send_formatted_message(message, FILE_TOO_LARGE_MESSAGE)
        return
    except Exception:
        logger.warning("calendar_download_failed")
        await send_formatted_message(message, DOWNLOAD_FAILED_MESSAGE)
        return

    service = calendar_import_service or CalendarImportService(db, SystemClock())
    data = buffer.getvalue()

    try:
        summary = await service.import_calendar(sender.id, document.file_name, data)
    except CalendarImportError as error:
        logger.info("calendar_import_rejected code=%s", error.code.value)
        await send_formatted_message(message, f"❌ {error.message}")
        return
    except Exception as error:
        logger.warning("calendar_import_failed code=%s", type(error).__name__)
        await send_formatted_message(message, IMPORT_FAILED_MESSAGE)
        return

    logger.info(
        "calendar_imported events=%d recurring=%d all_day=%d",
        summary.event_count,
        summary.recurring_count,
        summary.all_day_count,
    )
    await send_formatted_message(message, calendar_summary_text(summary))


def calendar_summary_text(summary: CalendarImportSummary) -> str:
    lines = ["✅ Личное расписание импортировано."]

    if summary.event_count == 0:
        lines.append("В файле нет событий; расписание сохранено пустым.")
    else:
        lines.append(f"Событий: {summary.event_count}.")
        if summary.recurring_count:
            lines.append(f"Повторяющихся: {summary.recurring_count}.")
        if summary.all_day_count:
            lines.append(f"На весь день: {summary.all_day_count}.")
        if summary.first_date is not None and summary.last_date is not None:
            if summary.first_date == summary.last_date:
                period = summary.first_date.strftime("%d.%m.%Y")
            else:
                period = (
                    f"{summary.first_date.strftime('%d.%m.%Y')} — "
                    f"{summary.last_date.strftime('%d.%m.%Y')}"
                )
            lines.append(f"Период: {period}.")

    lines.append(f"Часовой пояс: {summary.timezone}.")
    if summary.replaced:
        lines.append("Прежнее расписание заменено.")

    return "\n".join(lines)
