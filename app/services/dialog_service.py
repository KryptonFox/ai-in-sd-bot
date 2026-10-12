import asyncio
import logging

import asyncpg

from app.config import Settings
from app.core.exceptions import InvalidModeError, InvalidTemperatureError
from app.core.queue import UserQueueManager
from app.core.schemas.llm import LLMRequest
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.user import VALID_TEMPERATURES, UserSettings
from app.db.repositories.history_repo import (
    add_dialog_message,
    clear_dialog_history,
    delete_dialog_messages_by_ids,
    get_dialog_history,
    get_dialog_summary,
    get_user_messages_with_ids,
    set_dialog_summary,
)
from app.db.repositories.user_repo import get_or_create_user, set_user_mode, set_user_temperature
from app.llm.base import BaseLLMClient
from app.modes.base import BotMode
from app.modes.registry import ModeRegistry, registry

logger = logging.getLogger("app.services.dialog")


class DialogService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        settings: Settings,
        llm_client: BaseLLMClient,
        mode_registry: ModeRegistry = registry,
        queue_manager: UserQueueManager | None = None,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._llm = llm_client
        self._registry = mode_registry
        max_queue = getattr(settings, "user_queue_max_size", 3)
        self.queue_manager = queue_manager or UserQueueManager(max_queue_size=max_queue)

    async def _get_dynamic_history_chars(self) -> int:
        fn = getattr(self._llm, "get_context_length", None)
        if callable(fn):
            try:
                res = fn(self._settings.llm_model)
                if asyncio.iscoroutine(res):
                    ctx_len = await res
                elif isinstance(res, int):
                    ctx_len = res
                else:
                    ctx_len = getattr(self._settings, "default_context_length", 32768)
                return max(self._settings.history_max_chars, int(ctx_len * 0.60 * 2.5))
            except Exception:
                pass
        return self._settings.history_max_chars

    async def compact_history(self, user_id: int) -> bool:
        threshold = getattr(self._settings, "compaction_threshold_messages", 10)
        keep_recent = getattr(self._settings, "compaction_keep_recent", 3)

        messages_with_ids = await get_user_messages_with_ids(self._pool, user_id)
        if len(messages_with_ids) < threshold:
            return False

        to_compress = messages_with_ids[:-keep_recent]
        if not to_compress:
            return False

        existing_summary = await get_dialog_summary(self._pool, user_id)

        summary_prompt = (
            "Ты ассистент, выполняющий компактизацию истории диалога. Напиши краткую, "
            "структурированную выжимку контекста (summary) на русском языке: ключевые цели, "
            "выбранный стек технологий, принятые решения и текущее состояние беседы. "
            "Объём: 3-5 предложений без лишних вводных слов."
        )
        if existing_summary:
            summary_prompt += (
                f"\n\nПредыдущий контекст беседы:\n{existing_summary}\n\n"
                "Обнови выжимку с учётом новых сообщений."
            )

        messages_payload = [ChatMessage(role=MessageRole.SYSTEM, content=summary_prompt)]
        for _, msg in to_compress:
            messages_payload.append(msg)

        try:
            req = LLMRequest(
                messages=messages_payload,
                temperature=0.2,
                model=self._settings.llm_model,
            )
            resp = await self._llm.generate(req)
            new_summary = resp.content.strip()

            await set_dialog_summary(self._pool, user_id, new_summary)
            ids_to_delete = [mid for mid, _ in to_compress]
            await delete_dialog_messages_by_ids(self._pool, ids_to_delete)
            return True
        except Exception as exc:
            logger.warning(
                "Не удалось выполнить компактизацию для пользователя %d: %s", user_id, exc
            )
            return False

    async def process_user_message(self, user_id: int, user_text: str) -> str:
        user = await get_or_create_user(self._pool, user_id)
        mode = self._registry.get_or_default(user.mode)

        await self.compact_history(user_id)

        summary = await get_dialog_summary(self._pool, user_id)
        max_chars = await self._get_dynamic_history_chars()

        history = await get_dialog_history(
            self._pool,
            user_id,
            max_messages=self._settings.history_max_messages,
            max_chars=max_chars,
        )

        current_user_msg = ChatMessage(role=MessageRole.USER, content=user_text)

        initial_messages = mode.build_initial_messages()
        if summary:
            initial_messages.append(
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=f"Контекст предыдущей части диалога (выжимка):\n{summary}",
                )
            )

        messages = initial_messages + history + [current_user_msg]

        request = LLMRequest(
            messages=messages, temperature=user.temperature, model=self._settings.llm_model
        )
        response = await self._llm.generate(request)

        await add_dialog_message(self._pool, user_id, current_user_msg)
        await add_dialog_message(
            self._pool, user_id, ChatMessage(role=MessageRole.ASSISTANT, content=response.content)
        )

        return response.content

    async def switch_mode(self, user_id: int, mode_identifier: str) -> BotMode:
        async with self.queue_manager.acquire(user_id, ignore_limits=True):
            mode = self._registry.get(mode_identifier)
            if mode is None:
                raise InvalidModeError(f"Неизвестный режим: {mode_identifier}")

            await set_user_mode(self._pool, user_id, mode.id)
            await clear_dialog_history(self._pool, user_id)

            return mode

    async def reset_history(self, user_id: int) -> None:
        async with self.queue_manager.acquire(user_id, ignore_limits=True):
            await clear_dialog_history(self._pool, user_id)

    async def set_temperature(self, user_id: int, temperature: float) -> float:
        rounded = round(float(temperature), 1)
        if rounded not in VALID_TEMPERATURES:
            allowed = ", ".join(map(str, VALID_TEMPERATURES))
            raise InvalidTemperatureError(
                f"Недопустимое значение температуры: {temperature}. Разрешены: {allowed}"
            )

        await set_user_temperature(self._pool, user_id, rounded)

        return rounded

    async def get_user_status(self, user_id: int) -> tuple[UserSettings, BotMode]:
        user = await get_or_create_user(self._pool, user_id)
        mode = self._registry.get_or_default(user.mode)

        return (user, mode)
