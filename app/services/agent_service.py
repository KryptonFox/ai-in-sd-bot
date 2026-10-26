import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import ValidationError

from app.core.clock import Clock, SystemClock
from app.core.exceptions import LLMError
from app.core.ports import ToolClient
from app.core.schemas.agent import AgentDecision, AgentResult, EventContext, ResponseDecision
from app.core.schemas.llm import LLMRequest, LLMResponse, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure, WeatherResult
from app.core.timezones import normalize_reminder
from app.db.records import EventClaim
from app.db.repositories import event_repo
from app.db.repositories.audit_repo import record_decision
from app.llm.base import BaseLLMClient
from app.prompts.agent import agent_rules
from app.services.confirmation_service import ConfirmationService

FINISH_TOOL = ToolDefinition(
    name="finish_response",
    description="Завершить обычным ответом или структурированно запросить уточнение.",
    parameters=ResponseDecision.model_json_schema(),
)

CLARIFICATION_ERRORS = frozenset(
    {"timezone_required", "timezone_mismatch", "ambiguous_time", "past_time", "invalid_arguments"}
)


@dataclass
class _RunState:
    context: EventContext
    conversation: list[ChatMessage]
    tools: list[ToolDefinition]
    timezone: str | None
    temperature: float
    model: str | None
    dry_run: bool
    tools_available: bool
    started_at: float
    initial_calls: int
    calls_used: int
    last_call: ToolCall | None = None


@dataclass(frozen=True)
class _Outcome:
    result: AgentResult
    reason: str
    persist: bool = True


class AgentService:
    def __init__(
        self,
        llm: BaseLLMClient,
        mcp: ToolClient,
        confirmations: ConfirmationService | None = None,
        audit_pool: asyncpg.Pool | None = None,
        clock: Clock | None = None,
        max_tool_calls: int = 2,
        max_llm_steps: int = 4,
    ) -> None:
        self.llm = llm
        self.mcp = mcp
        self.confirmations = confirmations
        self.pool = audit_pool
        self.clock = clock or SystemClock()
        self.max_calls = min(max_tool_calls, 2)
        self.max_steps = max_llm_steps

    async def run(
        self,
        messages: Sequence[ChatMessage],
        context: EventContext,
        *,
        timezone: str | None = None,
        temperature: float = 0.7,
        model: str | None = None,
        dry_run: bool = False,
    ) -> AgentResult:
        started_at = time.monotonic()

        if self.confirmations is not None and not dry_run:
            existing = await self.confirmations.existing(context)
            if existing is not None:
                return existing

        event = await self._claim_event(context, dry_run=dry_run)
        if not event["claimed"]:
            return self._replay_event(event)

        state = self._create_state(
            messages,
            context,
            timezone=timezone,
            temperature=temperature,
            model=model,
            dry_run=dry_run,
            started_at=started_at,
            calls_used=event.get("calls_used", 0),
        )
        outcome = await self._run_with_errors(state)

        if not outcome.persist:
            return outcome.result

        return await self._record(context, outcome.result, started_at, outcome.reason)

    async def _claim_event(self, context: EventContext, *, dry_run: bool) -> EventClaim:
        if self.pool is None or dry_run:
            return {"claimed": True, "calls_used": 0}

        return await event_repo.begin(
            self.pool,
            context.user_id,
            context.event_id,
            self.clock.now(),
        )

    @staticmethod
    def _replay_event(event: EventClaim) -> AgentResult:
        result = event.get("result")
        if result is not None:
            return AgentResult.model_validate(result)

        return AgentResult(text="Этот запрос уже обрабатывается.")

    def _create_state(
        self,
        messages: Sequence[ChatMessage],
        context: EventContext,
        *,
        timezone: str | None,
        temperature: float,
        model: str | None,
        dry_run: bool,
        started_at: float,
        calls_used: int,
    ) -> _RunState:
        available = self.mcp.available
        now = self.clock.now()
        local_now = now.astimezone(ZoneInfo(timezone)) if timezone else now
        rules = agent_rules(local_now, timezone, tools_available=available)
        conversation = list(messages)

        for index, message in enumerate(conversation):
            if message.role == MessageRole.SYSTEM:
                conversation[index] = message.model_copy(
                    update={"content": message.content + "\n\n" + rules}
                )
                break

        return _RunState(
            context=context,
            conversation=conversation,
            tools=list(self.mcp.tools.values()) if available else [],
            timezone=timezone,
            temperature=temperature,
            model=model,
            dry_run=dry_run,
            tools_available=available,
            started_at=started_at,
            initial_calls=calls_used,
            calls_used=calls_used,
        )

    async def _run_with_errors(self, state: _RunState) -> _Outcome:
        try:
            return await self._run_steps(state)
        except ToolFailure as error:
            decision = "clarify" if error.code in CLARIFICATION_ERRORS else "error"
            return _Outcome(self._answer(state, error.message, decision), error.code)
        except LLMError as error:
            await self._record_model_failure(state.context, error)
            raise
        except (ValidationError, ValueError):
            return _Outcome(
                AgentResult(
                    text="Не удалось проверить действие. Уточните параметры.",
                    decision="clarify",
                ),
                "invalid_decision",
            )

    async def _run_steps(self, state: _RunState) -> _Outcome:
        for step in range(self.max_steps):
            response = await self.llm.generate(
                LLMRequest(
                    messages=state.conversation,
                    temperature=state.temperature,
                    model=state.model,
                    tools=[*state.tools, FINISH_TOOL],
                )
            )
            outcome = await self._handle_response(state, response, step)
            if outcome is not None:
                return outcome

        raise ToolFailure("step_budget", "Достигнут лимит шагов модели. Уточните запрос.")

    async def _handle_response(
        self,
        state: _RunState,
        response: LLMResponse,
        step: int,
    ) -> _Outcome | None:
        if not response.tool_calls:
            decision = "tool" if state.calls_used > state.initial_calls else "respond"
            return _Outcome(self._answer(state, response.content, decision), "plain_response")

        control = [call for call in response.tool_calls if call.name == FINISH_TOOL.name]
        if control:
            return self._finish_response(state, response.tool_calls, control[0])

        state.conversation.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )
        return await self._execute_calls(state, response.tool_calls, step)

    def _finish_response(
        self,
        state: _RunState,
        calls: Sequence[ToolCall],
        control: ToolCall,
    ) -> _Outcome:
        if len(calls) != 1:
            raise ToolFailure(
                "mixed_decision",
                "Ответ и вызовы инструментов должны идти отдельными шагами.",
            )

        decision = ResponseDecision.model_validate(control.arguments)
        if state.calls_used and decision.action == "respond":
            result = self._answer(state, decision.text, "tool")
        else:
            result = AgentResult(text=decision.text, decision=decision.action)

        return _Outcome(result, "structured_response")

    async def _execute_calls(
        self,
        state: _RunState,
        calls: Sequence[ToolCall],
        step: int,
    ) -> _Outcome | None:
        if sum(call.name == "add_reminder" for call in calls) > 1:
            raise ToolFailure("multiple_mutations", "Подтверждайте напоминания по одному.")

        for position, call in enumerate(calls):
            self._validate_call(state, call)
            state.last_call = call

            if call.name == "add_reminder":
                return await self._prepare_reminder(state, call, step * 100 + position)

            if state.dry_run:
                return _Outcome(
                    self._answer(state, "Выбран инструмент.", "tool"),
                    "dry_run",
                    persist=False,
                )

            outcome = await self._execute_read(state, call)
            if outcome is not None:
                return outcome

        return None

    def _validate_call(self, state: _RunState, call: ToolCall) -> None:
        if state.calls_used >= self.max_calls:
            raise ToolFailure("call_budget", "Достигнут лимит двух вызовов инструментов.")

        self.mcp.validate_arguments(call.name, call.arguments)

        if not state.tools_available:
            raise ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")

    async def _prepare_reminder(
        self,
        state: _RunState,
        call: ToolCall,
        position: int,
    ) -> _Outcome:
        normalized = normalize_reminder(call.arguments, state.timezone, self.clock.now())

        if state.dry_run:
            return _Outcome(
                AgentResult(
                    text="Подготовка напоминания.",
                    decision="prepare",
                    tool_name=call.name,
                    arguments=normalized.model_dump(mode="json"),
                ),
                "dry_run",
                persist=False,
            )

        if self.confirmations is None:
            raise ToolFailure("confirmation_unavailable", "Подтверждения сейчас недоступны.")

        result = await self.confirmations.prepare(
            state.context,
            call.arguments,
            state.timezone,
            position=position,
            calls_remaining=self.max_calls - state.calls_used,
        )
        return _Outcome(result, "awaiting_confirmation")

    async def _execute_read(self, state: _RunState, call: ToolCall) -> _Outcome | None:
        await self._reserve_call(state)
        payload = await self.mcp.call_tool(call.name, call.arguments)

        await self._record(
            state.context,
            self._answer(state, "", "tool"),
            state.started_at,
            "read_succeeded",
        )
        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=json.dumps({"source": call.name, "data": payload}, ensure_ascii=False),
            )
        )
        return self._location_clarification(state, payload)

    async def _reserve_call(self, state: _RunState) -> None:
        if self.pool is not None:
            reserved = await event_repo.reserve(
                self.pool,
                state.context.user_id,
                state.context.event_id,
                self.max_calls,
            )
            if not reserved:
                raise ToolFailure("call_budget", "Достигнут лимит вызовов исходного события.")

        state.calls_used += 1

    def _location_clarification(
        self,
        state: _RunState,
        payload: dict[str, object],
    ) -> _Outcome | None:
        status = payload.get("status")
        if status != "ambiguous" and status != "not_found":
            return None

        weather = WeatherResult.model_validate(payload)
        if weather.status == "ambiguous":
            candidates = "; ".join(
                f"{item.name}, {item.country}, {item.region or ''}" for item in weather.candidates
            )
            text = f"Уточните город: {candidates}."
        else:
            text = "Город не найден. Уточните название и страну."

        return _Outcome(self._answer(state, text, "clarify"), "location_clarification")

    @staticmethod
    def _answer(state: _RunState, text: str, decision: AgentDecision) -> AgentResult:
        call = state.last_call
        return AgentResult(
            text=text,
            decision=decision,
            tool_name=call.name if call else None,
            arguments=call.arguments if call else {},
        )

    async def _record_model_failure(self, context: EventContext, error: LLMError) -> None:
        if self.pool is None:
            return

        await event_repo.fail(self.pool, context.user_id, context.event_id, self.clock.now())
        await record_decision(
            self.pool,
            user_id=context.user_id,
            event_id=context.event_id,
            request_id=context.request_id,
            decision="error",
            validation=error.code,
            execution="failed",
            reason=error.code,
        )

    async def _record(
        self,
        context: EventContext,
        result: AgentResult,
        started_at: float,
        reason: str,
    ) -> AgentResult:
        if self.pool is None:
            return result

        await record_decision(
            self.pool,
            user_id=context.user_id,
            event_id=context.event_id,
            request_id=context.request_id,
            decision=result.decision,
            tool_name=result.tool_name,
            arguments=result.arguments,
            validation="ok" if result.decision not in {"error", "clarify"} else reason,
            execution=reason,
            reason=reason,
            duration_ms=(time.monotonic() - started_at) * 1000,
            created_at=self.clock.now(),
        )

        if reason != "read_succeeded":
            await event_repo.finish(
                self.pool,
                context.user_id,
                context.event_id,
                result.model_dump(mode="json"),
                self.clock.now(),
            )

        return result
