import asyncio
import logging

import asyncpg

from app.config import Settings
from app.core.clock import SystemClock
from app.core.context import get_request_id
from app.core.exceptions import InvalidModeError, InvalidTemperatureError, LLMServiceError
from app.core.queue import UserQueueManager
from app.core.schemas.agent import AgentResult, EventContext
from app.core.schemas.llm import LLMRequest
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.user import VALID_TEMPERATURES, UserSettings
from app.db.repositories.audit_repo import latest_decision
from app.db.repositories.history_repo import (
    add_dialog_message,
    clear_dialog_history,
    delete_dialog_messages_by_ids,
    get_dialog_history,
    get_dialog_summary,
    get_user_messages_with_ids,
    set_dialog_summary,
)
from app.db.repositories.timezone_repo import get_timezone, set_timezone
from app.db.repositories.user_repo import get_or_create_user, set_user_mode, set_user_temperature
from app.llm.base import BaseLLMClient
from app.modes.base import BotMode
from app.modes.registry import ModeRegistry, registry
from app.prompts.compaction import compaction_messages, summary_message

logger = logging.getLogger("app.services.dialog")


class DialogService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        settings: Settings,
        llm_client: BaseLLMClient,
        mode_registry: ModeRegistry = registry,
        queue_manager: UserQueueManager | None = None,
        agent=None,
        clock=None,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._llm = llm_client
        self._registry = mode_registry
        self.agent = agent
        self.clock = clock or SystemClock()
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

        messages_payload = compaction_messages(existing_summary, [msg for _, msg in to_compress])

        try:
            req = LLMRequest(
                messages=messages_payload,
                temperature=0.2,
                model=self._settings.llm_model,
            )
            resp = await self._llm.generate(req)
            new_summary = resp.content.strip()
            if not new_summary or resp.tool_calls or resp.finish_reason != "stop":
                raise LLMServiceError("Получена незавершённая выжимка истории.")

            await set_dialog_summary(self._pool, user_id, new_summary)
            ids_to_delete = [mid for mid, _ in to_compress]
            await delete_dialog_messages_by_ids(self._pool, ids_to_delete)
            return True
        except Exception:
            logger.warning("Не удалось выполнить компактизацию: generation_or_storage_error")
            return False

    async def process_user_message(
        self, user_id: int, user_text: str, *, event_id: str | None = None
    ) -> str | AgentResult:
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

        initial_messages = mode.build_initial_messages(user_text)
        if summary:
            initial_messages.append(summary_message(summary))

        safe_history = [
            msg
            if msg.role in {MessageRole.USER, MessageRole.ASSISTANT}
            else ChatMessage(
                role=MessageRole.USER,
                content="Недоверенные данные из истории:\n" + msg.content,
            )
            for msg in history
        ]
        messages = initial_messages + safe_history + [current_user_msg]

        if self.agent is not None:
            context = EventContext(
                user_id=user_id,
                event_id=event_id or f"local:{get_request_id()}",
                request_id=get_request_id() or "local",
            )
            result = await self.agent.run(
                messages,
                context,
                timezone=await get_timezone(self._pool, user_id),
                temperature=user.temperature,
                model=self._settings.llm_model,
            )
            await add_dialog_message(self._pool, user_id, current_user_msg)
            await add_dialog_message(
                self._pool, user_id, ChatMessage(role=MessageRole.ASSISTANT, content=result.text)
            )
            return result

        request = LLMRequest(
            messages=messages, temperature=user.temperature, model=self._settings.llm_model
        )
        response = await self._llm.generate(request)
        if response.tool_calls:
            raise LLMServiceError("Выполнение инструментов ещё не подключено.")
        answer = response.content
        if response.finish_reason == "length":
            answer += "\n\nОтвет обрезан из-за лимита генерации модели. Можно попросить продолжить."

        await add_dialog_message(self._pool, user_id, current_user_msg)
        await add_dialog_message(
            self._pool, user_id, ChatMessage(role=MessageRole.ASSISTANT, content=answer)
        )

        return answer

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

    async def tools_status(self):
        if self.agent is None or not self.agent.mcp.available:
            return "MCP-инструменты сейчас недоступны."
        lines = ["**Доступные инструменты:**"]
        for tool in self.agent.mcp.tools.values():
            access = "изменение, нужно подтверждение" if tool.name == "add_reminder" else "чтение"
            lines.append(f"- `{tool.name}` — {tool.description} ({access})")
        return "\n".join(lines)

    async def timezone_status(self, user_id, zone=None):
        from zoneinfo import ZoneInfo

        if zone is not None:
            ZoneInfo(zone)
            await set_timezone(self._pool, user_id, zone)
        else:
            zone = await get_timezone(self._pool, user_id)
        if zone is None:
            return "Часовой пояс не задан. Используйте /timezone Europe/Moscow."
        local_now = self.clock.now().astimezone(ZoneInfo(zone)).isoformat()
        return f"Часовой пояс: {zone}. Текущее время: {local_now}"

    async def why(self, user_id):
        record = await latest_decision(self._pool, user_id)
        if record is None:
            return "Решений пока нет. Отправьте запрос."
        return (
            f"Время: {record['created_at'].isoformat()}\nДействие: {record['decision']}\n"
            f"Инструмент: {record['tool_name'] or 'нет'}\nПараметры: {record['arguments']}\n"
            f"Проверка: {record['validation']}\nВыполнение: {record['execution']}\n"
            f"Длительность: {record['duration_ms']:.0f} мс\nПричина: {record['reason']}"
        )

    async def confirm_action(self, user_id, identifier, *, cancel=False):
        if self.agent is None or self.agent.confirmations is None:
            return AgentResult(text="Подтверждения недоступны.", decision="error")
        return await self.agent.confirmations.confirm(user_id, identifier, cancel=cancel)
