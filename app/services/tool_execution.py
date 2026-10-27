import json
import logging
import time
from collections.abc import Sequence

from app.core.clock import Clock, SystemClock
from app.core.ports import ToolClient, TurnStore
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    AwaitConfirmation,
    Continue,
    EventContext,
    Final,
    StepOutcome,
)
from app.core.schemas.execution import RunState
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.status import ToolStatus
from app.core.schemas.tools import ToolFailure, WeatherResult
from app.core.timezones import normalize_reminder
from app.services.confirmation_service import ConfirmationService

logger = logging.getLogger("app.tools")


class ToolExecutionService:
    def __init__(
        self,
        mcp: ToolClient | None = None,
        confirmations: ConfirmationService | None = None,
        store: TurnStore | None = None,
        *,
        clock: Clock | None = None,
        max_calls: int = 2,
        max_output_chars: int = 12000,
    ) -> None:
        self._mcp = mcp
        self._confirmations = confirmations
        self._store = store
        self._clock = clock or SystemClock()
        self._max_calls = min(max_calls, 2)
        self._max_output_chars = max_output_chars

    @property
    def available(self) -> bool:
        return self._mcp is not None and self._mcp.available

    @property
    def definitions(self) -> list[ToolDefinition]:
        return list(self._mcp.tools.values()) if self.available and self._mcp else []

    def status(self) -> list[ToolStatus]:
        return [
            ToolStatus(tool.name, tool.description, tool.name == "add_reminder")
            for tool in self.definitions
        ]

    async def existing(self, context: EventContext) -> AgentResult | None:
        return await self._confirmations.existing(context) if self._confirmations else None

    async def confirm(self, user_id: int, identifier: str, *, cancel: bool) -> AgentResult:
        if self._confirmations is None:
            return AgentResult(text="Подтверждения недоступны.", decision="error")
        return await self._confirmations.confirm(user_id, identifier, cancel=cancel)

    async def action_context(self, user_id: int) -> ChatMessage | None:
        return await self._confirmations.action_context(user_id) if self._confirmations else None

    async def execute(self, state: RunState, calls: Sequence[ToolCall], step: int) -> StepOutcome:
        if sum(call.name == "add_reminder" for call in calls) > 1:
            raise ToolFailure("multiple_mutations", "Подтверждайте напоминания по одному.")
        for position, call in enumerate(calls):
            self._validate(state, call)
            state.last_call = call
            if call.name == "add_reminder":
                return await self._prepare(state, call, step * 100 + position)
            if state.dry_run:
                return Final(
                    self._answer(state, "Выбран инструмент.", "tool", "dry_run", planned=True)
                )
            outcome = await self._read(state, call)
            if outcome is not None:
                return outcome
        return Continue()

    def _validate(self, state: RunState, call: ToolCall) -> None:
        if state.calls_used >= self._max_calls:
            raise ToolFailure("call_budget", "Достигнут лимит двух вызовов инструментов.")
        if self._mcp is None or not self.available:
            raise ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")
        if call.name not in {
            "get_weather",
            "get_schedule",
            "get_schedule_week",
            "read_resource",
            "add_reminder",
        }:
            raise ToolFailure("unknown_tool", "Инструмент не разрешён.")
        self._mcp.validate_arguments(call.name, call.arguments)

    async def _prepare(self, state: RunState, call: ToolCall, position: int) -> StepOutcome:
        normalized = normalize_reminder(call.arguments, state.timezone, self._clock.now())
        if state.dry_run:
            return Final(
                AgentResult(
                    text="Подготовка напоминания.",
                    decision="prepare",
                    tool_name=call.name,
                    arguments=normalized.model_dump(mode="json"),
                    planned=True,
                    reason="dry_run",
                )
            )
        if self._confirmations is None:
            raise ToolFailure("confirmation_unavailable", "Подтверждения сейчас недоступны.")
        result = await self._confirmations.prepare(
            state.context,
            call.arguments,
            state.timezone,
            position=position,
            calls_remaining=self._max_calls - state.calls_used,
        )
        return AwaitConfirmation(result.model_copy(update={"reason": "awaiting_confirmation"}))

    async def _read(self, state: RunState, call: ToolCall) -> Final | None:
        if self._store and not await self._store.reserve(state.context, self._max_calls):
            raise ToolFailure("call_budget", "Достигнут лимит вызовов исходного события.")
        state.calls_used += 1
        assert self._mcp is not None
        started = time.monotonic()
        payload = await self._mcp.call_tool(call.name, call.arguments)
        logger.info(
            "tool_complete request=%s tool=%s duration_ms=%.0f",
            state.context.request_id,
            call.name,
            (time.monotonic() - started) * 1000,
        )
        content = json.dumps({"source": call.name, "data": payload}, ensure_ascii=False)
        if len(content) > self._max_output_chars:
            raise ToolFailure(
                "tool_output_budget", "Результат инструмента слишком большой. Уточните запрос."
            )
        state.conversation.append(
            ChatMessage(role=MessageRole.TOOL, tool_call_id=call.id, content=content)
        )
        return self._weather_clarification(state, payload) if call.name == "get_weather" else None

    def _weather_clarification(self, state: RunState, payload: dict[str, object]) -> Final | None:
        if payload.get("status") not in {"ambiguous", "not_found"}:
            return None
        weather = WeatherResult.model_validate(payload)
        text = "Город не найден. Уточните название и страну."
        if weather.status == "ambiguous":
            candidates = "; ".join(
                f"{item.name}, {item.country}, {item.region or ''}" for item in weather.candidates
            )
            text = f"Уточните город: {candidates}."
        return Final(self._answer(state, text, "clarify", "location_clarification"))

    @staticmethod
    def _answer(
        state: RunState, text: str, decision: AgentDecision, reason: str, *, planned: bool = False
    ) -> AgentResult:
        return AgentResult(
            text=text,
            decision=decision,
            reason=reason,
            planned=planned,
            tool_name=state.last_call.name if state.last_call else None,
            arguments=state.last_call.arguments if state.last_call else {},
        )
