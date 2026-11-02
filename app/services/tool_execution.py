import json
import logging
import time
from collections.abc import Sequence

from app.core.clock import Clock, SystemClock
from app.core.ports import ToolClient, TurnStore
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
)
from app.core.schemas.execution import RunState
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure, WeatherResult, WeatherStatus
from app.core.timezones import normalize_reminder
from app.services.confirmation_service import ConfirmationService

logger = logging.getLogger("app.tools")
CONFIRMATION_POSITIONS_PER_STEP = 100


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

    async def execute(
        self,
        state: RunState,
        calls: Sequence[ToolCall],
        step: int,
    ) -> AgentResult | None:
        if sum(call.name == "add_reminder" for call in calls) > 1:
            raise ToolFailure("multiple_mutations", "Подтверждайте напоминания по одному.")

        for call_index, call in enumerate(calls):
            self._validate(state, call)
            state.last_call = call

            if call.name == "add_reminder":
                confirmation_position = step * CONFIRMATION_POSITIONS_PER_STEP + call_index
                return await self._prepare(state, call, confirmation_position)

            if state.dry_run:
                return state.build_result(
                    "Выбран инструмент.",
                    AgentDecision.TOOL,
                    "dry_run",
                    planned=True,
                )

            outcome = await self._read(state, call)
            if outcome is not None:
                return outcome

        return None

    def _validate(self, state: RunState, call: ToolCall) -> None:
        if state.calls_used >= self._max_calls:
            raise ToolFailure("call_budget", "Достигнут лимит двух вызовов инструментов.")

        if self._mcp is None or not self.available:
            raise ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")

        self._mcp.validate_arguments(call.name, call.arguments)

    async def _prepare(
        self,
        state: RunState,
        call: ToolCall,
        position: int,
    ) -> AgentResult:
        normalized = normalize_reminder(
            call.arguments,
            state.timezone,
            self._clock.now(),
        )
        if state.dry_run:
            return AgentResult(
                text="Подготовка напоминания.",
                decision=AgentDecision.PREPARE,
                tool_name=call.name,
                arguments=normalized.model_dump(mode="json"),
                planned=True,
                reason="dry_run",
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

        return result.model_copy(update={"reason": "awaiting_confirmation"})

    async def _read(self, state: RunState, call: ToolCall) -> AgentResult | None:
        payload = await self._call_read_tool(state, call)
        content = json.dumps({"source": call.name, "data": payload}, ensure_ascii=False)
        if len(content) > self._max_output_chars:
            raise ToolFailure(
                "tool_output_budget", "Результат инструмента слишком большой. Уточните запрос."
            )

        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=content,
            )
        )
        # todo отдельный инструмент для clarification
        if call.name == "get_weather":
            return self._weather_clarification(state, payload)

        return None

    async def _call_read_tool(
        self,
        state: RunState,
        call: ToolCall,
    ) -> dict[str, object]:
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

        return payload

    def _weather_clarification(
        self,
        state: RunState,
        payload: dict[str, object],
    ) -> AgentResult | None:
        if payload.get("status") not in {WeatherStatus.AMBIGUOUS, WeatherStatus.NOT_FOUND}:
            return None

        weather = WeatherResult.model_validate(payload)
        text = "Город не найден. Уточните название и страну."
        if weather.status == WeatherStatus.AMBIGUOUS:
            candidates = "; ".join(
                f"{item.name}, {item.country}, {item.region or ''}" for item in weather.candidates
            )
            text = f"Уточните город: {candidates}."

        return state.build_result(
            text,
            AgentDecision.CLARIFY,
            "location_clarification",
        )
