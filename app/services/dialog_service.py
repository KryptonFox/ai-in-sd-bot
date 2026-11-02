import asyncio
import logging
import time
import uuid
from zoneinfo import ZoneInfo

import asyncpg

from app.config import Settings
from app.core.clock import Clock, SystemClock
from app.core.context import get_request_id
from app.core.exceptions import (
    InterruptedRunError,
    InvalidModeError,
    InvalidTemperatureError,
    LLMError,
    LLMTimeoutError,
)
from app.core.ports import ToolClient, TurnStore
from app.core.queue import UserQueueManager
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    DialogResult,
    EventContext,
    InProgress,
    Replay,
)
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.status import TimezoneStatus, ToolStatus
from app.core.schemas.tools import ToolFailure
from app.core.schemas.user import VALID_TEMPERATURES, UserSettings
from app.db.records import DecisionAudit
from app.db.repositories.audit_repo import latest_decision
from app.db.repositories.history_repo import clear_dialog_history
from app.db.repositories.timezone_repo import get_timezone, set_timezone
from app.db.repositories.turn_repo import PostgresTurnStore
from app.db.repositories.user_repo import get_or_create_user, set_user_mode, set_user_temperature
from app.llm.base import BaseLLMClient
from app.modes.base import BotMode
from app.modes.registry import ModeRegistry, registry
from app.presentation.agent import reply_text
from app.services.agent_service import AgentService
from app.services.confirmation_service import ConfirmationService
from app.services.context_builder import ContextBuilder

logger = logging.getLogger("app.services.dialog")


class DialogService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        settings: Settings,
        llm_client: BaseLLMClient,
        mode_registry: ModeRegistry = registry,
        queue_manager: UserQueueManager | None = None,
        agent: AgentService | None = None,
        clock: Clock | None = None,
        *,
        confirmations: ConfirmationService | None = None,
        tool_client: ToolClient | None = None,
        store: TurnStore | None = None,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._registry = mode_registry
        self._clock = clock or SystemClock()
        self._queue = queue_manager or UserQueueManager(settings.user_queue_max_size)
        self._store = store or PostgresTurnStore(pool)
        self._confirmations = confirmations
        self._tool_client = tool_client
        self._agent = agent or AgentService(
            llm_client,
            clock=self._clock,
            max_llm_steps=settings.agent_max_llm_steps,
        )
        self._context = ContextBuilder(pool, settings, llm_client)

    @property
    def model_name(self) -> str:
        return self._settings.llm_model

    async def is_busy(self, user_id: int) -> bool:
        return await self._queue.is_user_busy(user_id)

    async def recover(self) -> None:
        await self._store.recover(self._clock.now())

    async def process_user_message(
        self,
        user_id: int,
        user_text: str,
        *,
        event_id: str | None = None,
    ) -> DialogResult:
        context = EventContext(
            user_id=user_id,
            event_id=event_id or f"local:{uuid.uuid4()}",
            request_id=get_request_id() or uuid.uuid4().hex,
        )

        async with self._queue.acquire(user_id):
            started = time.monotonic()
            deadline = asyncio.timeout(self._settings.agent_timeout)

            try:
                async with deadline:
                    claim = await self._store.claim(context, self._clock.now())
                    if isinstance(claim, Replay):
                        if claim.result.confirmation_id and self._confirmations is not None:
                            current = await self._confirmations.existing(context)
                            if current is not None:
                                return current

                        return claim.result

                    if isinstance(claim, InProgress):
                        return claim

                    return await self._process(
                        context,
                        user_text,
                        claim.calls_used,
                        started,
                    )
            except BaseException as error:
                reason = error.code if isinstance(error, LLMError) else type(error).__name__
                partial = await self._interrupt(context, str(reason))
                if isinstance(error, asyncio.CancelledError):
                    raise

                if partial:
                    raise InterruptedRunError from error

                if isinstance(error, TimeoutError) and deadline.expired():
                    raise LLMTimeoutError("Превышено общее время обработки запроса.") from None

                raise

    async def _process(
        self,
        context: EventContext,
        text: str,
        calls_used: int,
        started: float,
    ) -> AgentResult:
        existing = None
        if self._confirmations is not None:
            existing = await self._confirmations.existing(context)
        if existing is not None:
            result = existing
        else:
            result = await self._run_agent(context, text, calls_used)

        user_message = ChatMessage(role=MessageRole.USER, content=text)
        assistant_message = ChatMessage(role=MessageRole.ASSISTANT, content=reply_text(result))
        duration_ms = (time.monotonic() - started) * 1000
        await self._store.complete(
            context,
            user_message,
            assistant_message,
            result,
            self._clock.now(),
            duration_ms,
        )

        return result

    async def _run_agent(
        self,
        context: EventContext,
        text: str,
        calls_used: int,
    ) -> AgentResult:
        try:
            user = await get_or_create_user(self._pool, context.user_id)
            timezone = await get_timezone(self._pool, context.user_id)
            mode = self._registry.get_or_default(user.mode)
            action = None
            if self._confirmations is not None:
                action = await self._confirmations.action_context(context.user_id)

            prepared = await self._context.build(
                context.user_id,
                text,
                mode,
                self._agent.definitions,
                self._agent.instructions(timezone),
                action,
            )

            return await self._agent.run(
                prepared.messages,
                context,
                timezone=timezone,
                temperature=user.temperature,
                model=self.model_name,
                calls_used=calls_used,
                budget=prepared.budget,
            )
        except ToolFailure as error:
            return AgentResult(
                text=error.message,
                decision=AgentDecision.ERROR,
                reason=error.code,
            )

    async def _interrupt(self, context: EventContext, reason: str) -> bool:
        try:
            async with asyncio.timeout(3):
                return await self._store.interrupt(
                    context,
                    reason,
                    self._clock.now(),
                )
        except Exception:
            logger.warning("turn_cleanup_unavailable request=%s", context.request_id)
            return False

    async def compact_history(self, user_id: int) -> bool:
        async with self._queue.acquire(user_id, ignore_limits=True):
            return await self._context.compact(user_id)

    async def switch_mode(self, user_id: int, mode_identifier: str) -> BotMode:
        async with self._queue.acquire(user_id, ignore_limits=True):
            mode = self._registry.get(mode_identifier)
            if mode is None:
                raise InvalidModeError(f"Неизвестный режим: {mode_identifier}")

            await clear_dialog_history(self._pool, user_id)
            await set_user_mode(self._pool, user_id, mode.id)

            return mode

    async def reset_history(self, user_id: int) -> None:
        async with self._queue.acquire(user_id, ignore_limits=True):
            await clear_dialog_history(self._pool, user_id)

    async def set_temperature(self, user_id: int, temperature: float) -> float:
        rounded = round(float(temperature), 1)
        if rounded not in VALID_TEMPERATURES:
            raise InvalidTemperatureError("Допустимые температуры: 0.0, 0.3, 0.7, 1.0.")

        async with self._queue.acquire(user_id, ignore_limits=True):
            await set_user_temperature(self._pool, user_id, rounded)

        return rounded

    async def get_user_status(
        self,
        user_id: int,
    ) -> tuple[UserSettings, BotMode]:
        user = await get_or_create_user(self._pool, user_id)
        return user, self._registry.get_or_default(user.mode)

    async def tools_status(self) -> list[ToolStatus]:
        if self._tool_client is None or not self._tool_client.available:
            return []

        return [
            ToolStatus(tool.name, tool.description, tool.name == "add_reminder")
            for tool in self._tool_client.tools.values()
        ]

    async def timezone_status(
        self,
        user_id: int,
        zone: str | None = None,
    ) -> TimezoneStatus:
        if zone is not None:
            ZoneInfo(zone)

            async with self._queue.acquire(user_id, ignore_limits=True):
                await set_timezone(self._pool, user_id, zone)
        else:
            zone = await get_timezone(self._pool, user_id)

        now = self._clock.now()
        return TimezoneStatus(zone, now.astimezone(ZoneInfo(zone)) if zone else now)

    async def why(self, user_id: int) -> DecisionAudit | None:
        return await latest_decision(self._pool, user_id)

    async def confirm_action(
        self,
        user_id: int,
        identifier: str,
        *,
        cancel: bool = False,
    ) -> AgentResult:
        async with self._queue.acquire(user_id, ignore_limits=True):
            if self._confirmations is None:
                return AgentResult(text="Подтверждения недоступны.", decision=AgentDecision.ERROR)

            return await self._confirmations.confirm(user_id, identifier, cancel=cancel)
