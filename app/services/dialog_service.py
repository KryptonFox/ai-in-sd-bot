import asyncio
import inspect
import logging
import re
import time
import uuid
from collections.abc import Awaitable
from typing import TypeVar
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
from app.core.schemas.routing import RouteTarget
from app.core.schemas.specialist import (
    SpecialistIntent,
    SpecialistRequest,
    SpecialistResult,
    SpecialistSource,
)
from app.core.schemas.status import TimezoneStatus, ToolStatus
from app.core.schemas.tools import ToolFailure
from app.core.schemas.user import VALID_TEMPERATURES, UserSettings
from app.core.timezones import DEFAULT_USER_TIMEZONE
from app.core.trace import OperationTrace, bind_trace, current_trace, trace_step
from app.db.records import DecisionAudit
from app.db.repositories.audit_repo import latest_decision
from app.db.repositories.history_repo import clear_dialog_history, get_dialog_history
from app.db.repositories.timezone_repo import get_timezone, set_timezone
from app.db.repositories.trace_repo import save_trace
from app.db.repositories.turn_repo import PostgresTurnStore
from app.db.repositories.user_repo import get_or_create_user, set_user_mode, set_user_temperature
from app.llm.base import BaseLLMClient
from app.mcp.request_context import MCPRequestContext, bind_mcp_request
from app.modes.base import BotMode
from app.modes.registry import ModeRegistry, registry
from app.presentation.agent import reply_text
from app.services.agent_service import AgentService
from app.services.confirmation_service import ConfirmationService
from app.services.context_builder import ContextBuilder
from app.services.knowledge_response import ground_knowledge_response
from app.services.supervisor import RouteSelection, Supervisor

logger = logging.getLogger("app.services.dialog")
TURN_STORE_TIMEOUT_SECONDS = 3.0
T = TypeVar("T")
_TRANSLATE_PREFIX = re.compile(r"^\s*переведи(?:те)?\b", re.IGNORECASE)
_PROMPT_DISCLOSURE_REQUEST = re.compile(
    r"\b(?:покажи(?:те)?|выведи(?:те)?|раскрой(?:те)?|напечатай(?:те)?|повтори(?:те)?|"
    r"процитируй(?:те)?|пришли(?:те)?|отдай(?:те)?|скажи(?:те)?|озвучь(?:те)?|"
    r"show|reveal|print|repeat|dump|display|output)\b"
    r"[^.!?\n]{0,100}"
    r"(?:\b(?:свой|свои|тво[йюи]|your|hidden|internal|скрыт\w*|внутренн\w*)\s+"
    r"(?:системн\w+\s+)?(?:промпт\w*|инструкц\w*|правил\w*|prompt|instructions?)\b"
    r"|\b(?:системн\w+\s+(?:промпт\w*|инструкц\w*)|system\s+prompt|developer\s+prompt)"
    r"\s+(?:бота|ассистента|целиком|дословно|verbatim)\b)",
    re.IGNORECASE,
)


def _is_prompt_disclosure_request(text: str) -> bool:
    if _TRANSLATE_PREFIX.search(text) is not None:
        return False
    return _PROMPT_DISCLOSURE_REQUEST.search(text) is not None


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
        supervisor: Supervisor | None = None,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._registry = mode_registry
        self._clock = clock or SystemClock()
        self._queue = queue_manager or UserQueueManager(settings.user_queue_max_size)
        self._store = store or PostgresTurnStore(pool)
        self._confirmations = confirmations
        self._tool_client = tool_client
        self._supervisor = supervisor
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
            deadline_at = asyncio.get_running_loop().time() + self._settings.agent_timeout

            try:
                claim = await self._bounded(
                    self._store.claim(context, self._clock.now()),
                    deadline_at,
                )
                if isinstance(claim, Replay):
                    if claim.result.confirmation_id and self._confirmations is not None:
                        current = await self._bounded(
                            self._confirmations.existing(context),
                            deadline_at,
                        )
                        if current is not None:
                            return current.model_copy(update={"images": list(claim.result.images)})

                    return claim.result

                if isinstance(claim, InProgress):
                    return claim

                return await self._process_with_trace(
                    context,
                    user_text,
                    claim.calls_used,
                    started,
                    deadline_at,
                )
            except BaseException as error:
                reason = error.code if isinstance(error, LLMError) else type(error).__name__
                partial = await self._interrupt(context, str(reason))
                if isinstance(error, asyncio.CancelledError):
                    raise

                if partial:
                    raise InterruptedRunError from error

                raise

    async def _process_with_trace(
        self, context: EventContext, text: str, calls_used: int, started: float, deadline_at: float
    ) -> AgentResult:
        if self._supervisor is None:
            return await self._process(context, text, calls_used, started, deadline_at)

        trace = OperationTrace(context.user_id, context.request_id, self._clock.now())
        try:
            with bind_trace(trace), trace_step("dialog", "receive") as step:
                result = await self._process(context, text, calls_used, started, deadline_at)
                if step is not None and result.decision is AgentDecision.ERROR:
                    step.fail(result.reason)
                return result
        finally:
            try:
                async with asyncio.timeout(1):
                    await save_trace(self._pool, trace)
            except Exception as error:
                logger.warning("trace_store_unavailable code=%s", type(error).__name__)

    async def _bounded(self, operation: Awaitable[T], deadline_at: float) -> T:
        """Run one preparation stage inside the request deadline."""
        if deadline_at <= asyncio.get_running_loop().time():
            if inspect.iscoroutine(operation):
                operation.close()
            raise LLMTimeoutError("Превышено общее время обработки запроса.")

        timeout = asyncio.timeout_at(deadline_at)
        try:
            async with timeout:
                return await operation
        except TimeoutError:
            if timeout.expired():
                raise LLMTimeoutError("Превышено общее время обработки запроса.") from None
            raise

    async def _process(
        self,
        context: EventContext,
        text: str,
        calls_used: int,
        started: float,
        deadline_at: float,
    ) -> AgentResult:
        existing = None
        if self._confirmations is not None:
            existing = await self._bounded(
                self._confirmations.existing(context),
                deadline_at,
            )
        if existing is not None:
            result = existing
        else:
            result = await self._run_agent(context, text, calls_used, deadline_at)

        user_message = ChatMessage(role=MessageRole.USER, content=text)
        metadata: dict[str, object] = {}
        if result.reason == "web_search_offer" and result.decision in {
            AgentDecision.RESPOND,
            AgentDecision.TOOL,
        }:
            metadata["web_search_offer"] = True
        elif result.reason == "reminder_followup" and result.decision is AgentDecision.CLARIFY:
            metadata["reminder_followup"] = True
        assistant_message = ChatMessage(
            role=MessageRole.ASSISTANT, content=reply_text(result), metadata=metadata
        )
        duration_ms = (time.monotonic() - started) * 1000
        await self._complete(
            context,
            user_message,
            assistant_message,
            result,
            duration_ms,
        )

        return result

    async def _run_agent(
        self,
        context: EventContext,
        text: str,
        calls_used: int,
        deadline_at: float,
    ) -> AgentResult:
        try:
            user = await self._bounded(
                get_or_create_user(self._pool, context.user_id),
                deadline_at,
            )
            timezone = (
                await self._bounded(
                    get_timezone(self._pool, context.user_id),
                    deadline_at,
                )
            ) or DEFAULT_USER_TIMEZONE
            if _is_prompt_disclosure_request(text):
                with trace_step("guard", "prompt_disclosure") as guard_step:
                    if guard_step is not None:
                        guard_step.fail("prompt_disclosure_blocked")
                trace = current_trace()
                if trace is not None:
                    trace.route = RouteTarget.LEGACY.value
                return AgentResult(
                    text="Я не могу раскрывать системные инструкции.",
                    decision=AgentDecision.RESPOND,
                    reason="prompt_disclosure_blocked",
                )
            selection = await self._select_agent(context, text, deadline_at)
            if selection.agent is None:
                return AgentResult(
                    text="Уточните, что нужно сделать в первую очередь.",
                    decision=AgentDecision.CLARIFY,
                    reason="route_clarification",
                )
            trace = current_trace()
            if trace is None:
                with bind_mcp_request(MCPRequestContext(context.user_id)):
                    return await self._invoke_agent(
                        selection.agent, user, timezone, context, text, calls_used, deadline_at
                    )

            request = SpecialistRequest(
                actor_id=context.user_id,
                request_id=context.request_id,
                trace_id=trace.id,
                started_at=trace.started_at,
                timezone=ZoneInfo(timezone),
                text=text,
                intent=SpecialistIntent.USER_QUERY,
                source=SpecialistSource.TELEGRAM,
            )
            mcp_request = MCPRequestContext(
                request.actor_id,
                selection.web_allowed,
                selection.reminder_allowed,
            )
            with bind_mcp_request(mcp_request), trace_step("specialist", selection.target) as step:
                result = await self._invoke_agent(
                    selection.agent,
                    user,
                    request.timezone.key,
                    context,
                    request.text,
                    calls_used,
                    deadline_at,
                )
                if selection.target is RouteTarget.KNOWLEDGE:
                    with trace_step("knowledge", "ground") as ground_step:
                        result = ground_knowledge_response(
                            result, mcp_request.facts, mcp_request.attempted_sources
                        )
                        if ground_step is not None and result.decision is AgentDecision.ERROR:
                            ground_step.fail(result.reason)
                if (
                    selection.target is RouteTarget.SCHEDULE
                    and selection.reminder_allowed
                    and result.decision is AgentDecision.CLARIFY
                ):
                    result = result.model_copy(update={"reason": "reminder_followup"})
                if step is not None and result.decision is AgentDecision.ERROR:
                    step.fail(result.reason)
                return SpecialistResult(
                    status=result.decision,
                    public_text=result.text,
                    facts=tuple(mcp_request.facts),
                    used_tools=tuple(sorted(mcp_request.attempted_sources)),
                    error_code=result.reason if result.decision is AgentDecision.ERROR else None,
                    agent_result=result,
                ).to_agent_result()
        except ToolFailure as error:
            return AgentResult(
                text=error.message,
                decision=AgentDecision.ERROR,
                reason=error.code,
            )

    async def _select_agent(
        self, context: EventContext, text: str, deadline_at: float
    ) -> RouteSelection:
        if self._supervisor is None:
            return RouteSelection(RouteTarget.LEGACY, self._agent)

        history = await self._bounded(
            get_dialog_history(self._pool, context.user_id, max_messages=4, max_chars=2000),
            deadline_at,
        )
        with trace_step("router", "choice", model_name=self._supervisor.model) as step:
            selection = await self._bounded(self._supervisor.select(text, history), deadline_at)
            if step is not None and not selection.router_invoked:
                step.component = "supervisor"
                step.action = "offer_approval"
                step.model_name = None
        trace = current_trace()
        if trace is not None:
            trace.route = (
                RouteTarget.CLARIFY.value if selection.agent is None else selection.target.value
            )
        return selection

    async def _invoke_agent(
        self,
        agent: AgentService,
        user: UserSettings,
        timezone: str,
        context: EventContext,
        text: str,
        calls_used: int,
        deadline_at: float,
    ) -> AgentResult:
        action = None
        if self._confirmations is not None:
            action = await self._bounded(
                self._confirmations.action_context(context.user_id), deadline_at
            )
        prepared = await self._bounded(
            self._context.build(
                context.user_id,
                text,
                self._registry.get_or_default(user.mode),
                agent.definitions,
                agent.instructions(timezone),
                action,
            ),
            deadline_at,
        )
        return await agent.run(
            prepared.messages,
            context,
            timezone=timezone,
            temperature=user.temperature,
            model=self.model_name,
            calls_used=calls_used,
            budget=prepared.budget,
            protected_prefix=prepared.protected_prefix,
            deadline_at=deadline_at,
        )

    async def _complete(
        self,
        context: EventContext,
        user_message: ChatMessage,
        assistant_message: ChatMessage,
        result: AgentResult,
        duration_ms: float,
    ) -> None:
        try:
            async with asyncio.timeout(TURN_STORE_TIMEOUT_SECONDS):
                await self._store.complete(
                    context,
                    user_message,
                    assistant_message,
                    result,
                    self._clock.now(),
                    duration_ms,
                )
        except TimeoutError:
            logger.warning("turn_completion_timeout request=%s", context.request_id)
            raise

    async def _interrupt(self, context: EventContext, reason: str) -> bool:
        try:
            async with asyncio.timeout(TURN_STORE_TIMEOUT_SECONDS):
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
            ToolStatus(
                tool.name,
                tool.description,
                requires_confirmation=not self._tool_client.is_read_only(tool.name),
            )
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
            zone = await get_timezone(self._pool, user_id) or DEFAULT_USER_TIMEZONE

        now = self._clock.now()
        return TimezoneStatus(zone, now.astimezone(ZoneInfo(zone)))

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
