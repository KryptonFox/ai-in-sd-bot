from collections.abc import Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.budget import ContextBudget
from app.core.clock import Clock, SystemClock
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    AwaitConfirmation,
    EventContext,
    Final,
    ResponseDecision,
    StepOutcome,
)
from app.core.schemas.execution import RunState
from app.core.schemas.llm import LLMRequest, LLMResponse, ToolDefinition
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
    {"timezone_required", "timezone_mismatch", "ambiguous_time", "past_time", "invalid_arguments"}
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
        return agent_rules(local, timezone, tools_available=self._tools.available)

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
        state = RunState(context, list(messages), timezone, dry_run, calls_used)
        self._append_instructions(state)
        try:
            return await self._run_steps(state, temperature, model, budget or ContextBudget(32768))
        except ToolFailure as error:
            decision = "clarify" if error.code in CLARIFICATION_ERRORS else "error"
            return self._answer(state, error.message, decision, error.code)
        except (ValidationError, ValueError):
            return self._answer(
                state,
                "Не удалось проверить действие. Уточните параметры.",
                "clarify",
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
        self, state: RunState, temperature: float, model: str | None, budget: ContextBudget
    ) -> AgentResult:
        definitions = self.definitions
        for step in range(self._max_steps):
            budget.ensure(state.conversation, definitions)
            response = await self._llm.generate(
                LLMRequest(
                    messages=state.conversation,
                    temperature=temperature,
                    model=model,
                    tools=definitions,
                    max_tokens=budget.output_tokens,
                )
            )
            outcome = await self._handle_response(state, response, step)
            if isinstance(outcome, (Final, AwaitConfirmation)):
                return outcome.result
        raise ToolFailure("step_budget", "Достигнут лимит шагов модели. Уточните запрос.")

    async def _handle_response(
        self, state: RunState, response: LLMResponse, step: int
    ) -> StepOutcome:
        if response.finish_reason == "length":
            if response.tool_calls:
                return Final(
                    self._answer(
                        state,
                        "Модель не завершила подготовку действия. Уточните запрос.",
                        "error",
                        "truncated_tool_call",
                    )
                )
            result = self._answer(state, response.content, "respond", "truncated_response")
            return Final(result.model_copy(update={"truncated": True}))
        if not response.tool_calls:
            decision = "tool" if state.calls_used else "respond"
            return Final(self._answer(state, response.content, decision, "plain_response"))
        control = next(
            (call for call in response.tool_calls if call.name == FINISH_TOOL.name), None
        )
        if control:
            return self._finish_response(state, response.tool_calls, control)
        state.conversation.append(
            ChatMessage(
                role=MessageRole.ASSISTANT, content=response.content, tool_calls=response.tool_calls
            )
        )
        return await self._tools.execute(state, response.tool_calls, step)

    def _finish_response(
        self, state: RunState, calls: Sequence[ToolCall], control: ToolCall
    ) -> Final:
        if len(calls) != 1:
            raise ToolFailure(
                "mixed_decision", "Ответ и вызовы инструментов должны идти отдельными шагами."
            )
        decision = ResponseDecision.model_validate(control.arguments)
        action = "tool" if state.calls_used and decision.action == "respond" else decision.action
        return Final(self._answer(state, decision.text, action, "structured_response"))

    @staticmethod
    def _answer(state: RunState, text: str, decision: AgentDecision, reason: str) -> AgentResult:
        return AgentResult(
            text=text,
            decision=decision,
            reason=reason,
            tool_name=state.last_call.name if state.last_call else None,
            arguments=state.last_call.arguments if state.last_call else {},
        )
