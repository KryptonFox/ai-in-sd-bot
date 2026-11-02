from collections.abc import Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.budget import ContextBudget
from app.core.clock import Clock, SystemClock
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    EventContext,
    ResponseDecision,
)
from app.core.schemas.execution import RunState
from app.core.schemas.llm import FinishReason, LLMRequest, LLMResponse, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure
from app.llm.base import BaseLLMClient
from app.prompts.agent import agent_rules
from app.services.tool_execution import ToolExecutionService

FINISH_TOOL = ToolDefinition(
    name="finish_response",
    description="Завершить обычным ответом или структурированно запросить уточнение.",
    parameters=ResponseDecision.model_json_schema(),
)
CLARIFICATION_ERRORS = frozenset(
    {
        "timezone_required",
        "timezone_mismatch",
        "ambiguous_time",
        "past_time",
        "invalid_arguments",
        "forecast_date_unavailable",
    }
)


class AgentService:
    def __init__(
        self,
        llm: BaseLLMClient,
        tools: ToolExecutionService | None = None,
        *,
        clock: Clock | None = None,
        max_llm_steps: int = 4,
    ) -> None:
        self._llm = llm
        self._tools = tools or ToolExecutionService(clock=clock)
        self._clock = clock or SystemClock()
        self._max_steps = max_llm_steps

    @property
    def definitions(self) -> list[ToolDefinition]:
        return [*self._tools.definitions, FINISH_TOOL]

    def instructions(self, timezone: str | None) -> str:
        now = self._clock.now()
        local = now.astimezone(ZoneInfo(timezone)) if timezone else now
        return agent_rules(
            local,
            timezone,
            tools_available=self._tools.available,
        )

    async def run(
        self,
        messages: Sequence[ChatMessage],
        context: EventContext,
        *,
        timezone: str | None = None,
        temperature: float = 0.7,
        model: str | None = None,
        dry_run: bool = False,
        calls_used: int = 0,
        budget: ContextBudget | None = None,
    ) -> AgentResult:
        state = RunState(
            context=context,
            conversation=list(messages),
            timezone=timezone,
            dry_run=dry_run,
            calls_used=calls_used,
        )
        self._append_instructions(state)

        try:
            return await self._run_steps(
                state,
                temperature,
                model,
                budget or ContextBudget(32768),
            )
        except ToolFailure as error:
            decision = (
                AgentDecision.CLARIFY if error.code in CLARIFICATION_ERRORS else AgentDecision.ERROR
            )
            return state.build_result(error.message, decision, error.code)
        except (ValidationError, ValueError):
            return state.build_result(
                "Не удалось проверить действие. Уточните параметры.",
                AgentDecision.CLARIFY,
                "invalid_decision",
            )

    def _append_instructions(self, state: RunState) -> None:
        instructions = self.instructions(state.timezone)

        for index, message in enumerate(state.conversation):
            if message.role == MessageRole.SYSTEM:
                state.conversation[index] = message.model_copy(
                    update={"content": message.content + "\n\n" + instructions}
                )
                return

        state.conversation.insert(0, ChatMessage(role=MessageRole.SYSTEM, content=instructions))

    async def _run_steps(
        self,
        state: RunState,
        temperature: float,
        model: str | None,
        budget: ContextBudget,
    ) -> AgentResult:
        """Run bounded model steps; a None outcome requests another step."""
        definitions = self.definitions

        for step in range(self._max_steps):
            output_limit = budget.generation_limit(state.conversation, definitions)
            response = await self._llm.generate(
                LLMRequest(
                    messages=state.conversation,
                    temperature=temperature,
                    model=model,
                    tools=definitions,
                    max_tokens=output_limit,
                )
            )

            outcome = await self._handle_response(state, response, step)
            if outcome is not None:
                return outcome

        raise ToolFailure("step_budget", "Достигнут лимит шагов модели. Уточните запрос.")

    async def _handle_response(
        self,
        state: RunState,
        response: LLMResponse,
        step: int,
    ) -> AgentResult | None:
        if response.finish_reason == FinishReason.LENGTH:
            if response.tool_calls:
                return state.build_result(
                    "Модель не завершила подготовку действия. Уточните запрос.",
                    AgentDecision.ERROR,
                    "truncated_tool_call",
                )

            result = state.build_result(
                response.content,
                AgentDecision.RESPOND,
                "truncated_response",
                truncated=True,
            )
            return result

        if not response.tool_calls:
            decision = AgentDecision.TOOL if state.calls_used else AgentDecision.RESPOND
            return state.build_result(
                response.content,
                decision,
                "plain_response",
            )

        for call in response.tool_calls:
            if call.name == FINISH_TOOL.name:
                return self._finish_response(state, response.tool_calls, call)

        state.conversation.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        return await self._tools.execute(state, response.tool_calls, step)

    def _finish_response(
        self,
        state: RunState,
        calls: Sequence[ToolCall],
        control: ToolCall,
    ) -> AgentResult:
        if len(calls) != 1:
            raise ToolFailure(
                "mixed_decision", "Ответ и вызовы инструментов должны идти отдельными шагами."
            )

        decision = ResponseDecision.model_validate(control.arguments)
        action: AgentDecision = decision.action
        if state.calls_used and action == AgentDecision.RESPOND:
            action = AgentDecision.TOOL

        return state.build_result(
            decision.text,
            action,
            "structured_response",
        )
