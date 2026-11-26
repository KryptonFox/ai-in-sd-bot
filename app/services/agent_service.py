import asyncio
import logging
from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.budget import ContextBudget
from app.core.clock import Clock, SystemClock
from app.core.conversation import complete_unit_indices
from app.core.exceptions import LLMTimeoutError
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    ClarificationArguments,
    EventContext,
)
from app.core.schemas.execution import RunState, StepAction
from app.core.schemas.llm import (
    FinishReason,
    LLMRequest,
    LLMResponse,
    ToolDefinition,
)
from app.core.schemas.mcp import ReadResourceArguments, ResourceDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure
from app.core.trace import trace_step
from app.llm.base import BaseLLMClient
from app.prompts.agent import agent_rules
from app.services.tool_execution import READ_RESOURCE_FUNCTION, ToolExecutionService

logger = logging.getLogger("app.services.agent")
REQUEST_DEADLINE_MESSAGE = "Превышено общее время обработки запроса."

CLARIFICATION_TOOL = ToolDefinition(
    name="clarification",
    description="Задать пользователю уточняющий вопрос, если необходимых данных не хватает.",
    parameters=ClarificationArguments.model_json_schema(),
)
CLARIFICATION_ERRORS = frozenset(
    {
        "timezone_required",
        "timezone_mismatch",
        "ambiguous_time",
        "past_time",
        "invalid_time",
        "invalid_arguments",
        "forecast_date_unavailable",
    }
)


def _resource_reader_definition(resources: Mapping[str, ResourceDefinition]) -> ToolDefinition:
    parameters = ReadResourceArguments.model_json_schema()
    parameters["properties"]["uri"]["enum"] = sorted(resources)
    descriptions = [
        f"{resource.uri}: {resource.name}. {resource.description}"
        for resource in resources.values()
    ]
    return ToolDefinition(
        name=READ_RESOURCE_FUNCTION,
        description=(
            "Прочитать данные обнаруженного MCP-ресурса. "
            "Приложение выполняет resources/read; это локальная функция, "
            "а не инструмент MCP-сервера. Доступные ресурсы:\n" + "\n".join(descriptions)
        ),
        parameters=parameters,
    )


class AgentService:
    def __init__(
        self,
        llm: BaseLLMClient,
        tools: ToolExecutionService | None = None,
        *,
        clock: Clock | None = None,
        max_llm_steps: int = 4,
        specialist_instructions: str = "",
    ) -> None:
        self._llm = llm
        self._tools = tools or ToolExecutionService()
        self._clock = clock or SystemClock()
        self._max_steps = max_llm_steps
        self._specialist_instructions = specialist_instructions

    @property
    def definitions(self) -> list[ToolDefinition]:
        definitions = self._tools.definitions
        local_names = {CLARIFICATION_TOOL.name, READ_RESOURCE_FUNCTION}
        if any(tool.name in local_names for tool in definitions):
            raise ToolFailure(
                "capability_conflict", "Имя MCP-инструмента занято локальной функцией."
            )
        if self._tools.resources:
            definitions.append(_resource_reader_definition(self._tools.resources))
        return [*definitions, CLARIFICATION_TOOL]

    def instructions(self, timezone: str | None) -> str:
        now = self._clock.now()
        local = now.astimezone(ZoneInfo(timezone)) if timezone else now
        rules = agent_rules(local, timezone, tools_available=self._tools.available)
        if self._specialist_instructions:
            return f"{rules}\n\n{self._specialist_instructions}"
        return rules

    async def run(
        self,
        messages: Sequence[ChatMessage],
        context: EventContext,
        *,
        timezone: str | None = None,
        temperature: float = 0.7,
        model: str | None = None,
        calls_used: int = 0,
        budget: ContextBudget | None = None,
        protected_prefix: int = 0,
        deadline_at: float | None = None,
    ) -> AgentResult:
        state = RunState(
            context=context,
            conversation=list(messages),
            timezone=timezone,
            calls_used=calls_used,
            protected_prefix=protected_prefix,
            deadline_at=deadline_at,
        )
        self._append_instructions(state)
        budget = budget or ContextBudget(32768)

        try:
            return await self._run_steps(state, temperature, model, budget)
        except ToolFailure as error:
            decision = (
                AgentDecision.CLARIFY if error.code in CLARIFICATION_ERRORS else AgentDecision.ERROR
            )
            reason = (
                "tool_failed"
                if error.code in {"web_search_offer", "reminder_followup"}
                else error.code
            )
            return state.build_result(error.message, decision, reason)

    def _append_instructions(self, state: RunState) -> None:
        instructions = self.instructions(state.timezone)

        for index, message in enumerate(state.conversation):
            if message.role == MessageRole.SYSTEM:
                state.conversation[index] = message.model_copy(
                    update={"content": message.content + "\n\n" + instructions}
                )
                return

        state.protected_prefix += 1
        state.conversation.insert(0, ChatMessage(role=MessageRole.SYSTEM, content=instructions))

    async def _run_steps(
        self,
        state: RunState,
        temperature: float,
        model: str | None,
        budget: ContextBudget,
    ) -> AgentResult:
        definitions = self.definitions

        for _ in range(self._max_steps):
            try:
                output_limit = budget.generation_limit(state.conversation, definitions)
            except ToolFailure as error:
                if error.code != "context_budget":
                    raise

                if not self._trim_history_for_reserve(state, definitions, budget):
                    raise

                output_limit = budget.generation_limit(state.conversation, definitions)

            with trace_step("llm", "generate", model_name=model) as step:
                response = await self._generate(
                    state,
                    LLMRequest(
                        messages=state.conversation,
                        temperature=temperature,
                        model=model,
                        tools=definitions,
                        max_tokens=output_limit,
                    ),
                )
                if step is not None:
                    step.input_tokens = response.prompt_tokens
                    step.output_tokens = response.completion_tokens
            outcome = await self._handle_response(state, response)
            if isinstance(outcome, AgentResult):
                return outcome

        raise ToolFailure("step_budget", "Достигнут лимит шагов модели. Уточните запрос.")

    async def _generate(self, state: RunState, request: LLMRequest) -> LLMResponse:
        if state.deadline_at is None:
            return await self._llm.generate(request)
        if state.deadline_at <= asyncio.get_running_loop().time():
            raise LLMTimeoutError(REQUEST_DEADLINE_MESSAGE)

        timeout = asyncio.timeout_at(state.deadline_at)
        try:
            async with timeout:
                return await self._llm.generate(request)
        except TimeoutError:
            if timeout.expired():
                raise LLMTimeoutError(REQUEST_DEADLINE_MESSAGE) from None
            raise

    def _trim_history_for_reserve(
        self,
        state: RunState,
        definitions: Sequence[ToolDefinition],
        budget: ContextBudget,
    ) -> bool:
        conversation = state.conversation
        last_user = next(
            (
                index
                for index in range(len(conversation) - 1, -1, -1)
                if conversation[index].role == MessageRole.USER
            ),
            None,
        )
        protected_prefix = min(state.protected_prefix, len(conversation))

        if last_user is None or last_user <= protected_prefix:
            return False

        prefix = conversation[:protected_prefix]
        history = conversation[protected_prefix:last_user]
        current_turn = conversation[last_user:]
        units = complete_unit_indices(history)
        pinned = [message for message in history if message.role == MessageRole.SYSTEM]

        for dropped_units in range(1, len(units) + 1):
            remaining = [history[index] for unit in units[dropped_units:] for index in unit]
            candidate = [*prefix, *pinned, *remaining, *current_turn]
            if not budget.has_generation_reserve(candidate, definitions):
                continue

            state.conversation = candidate
            logger.info(
                "context_history_trim dropped_units=%d remaining_units=%d output=%d reserve=%d",
                dropped_units,
                len(units) - dropped_units,
                budget.generation_limit(candidate, definitions),
                budget.effective_generation_reserve,
            )
            return True

        return False

    async def _handle_response(
        self,
        state: RunState,
        response: LLMResponse,
    ) -> AgentResult | StepAction:
        if response.finish_reason == FinishReason.LENGTH:
            if response.tool_calls:
                return state.build_result(
                    "Модель не завершила подготовку действия. Уточните запрос.",
                    AgentDecision.ERROR,
                    "truncated_tool_call",
                )

            if self._contains_system_leak(state, response.content):
                return state.build_result(
                    "Я не могу раскрывать системные инструкции.",
                    AgentDecision.ERROR,
                    "prompt_leak_blocked",
                )

            return state.build_result(
                response.content,
                AgentDecision.RESPOND,
                "truncated_response",
                truncated=True,
            )

        if not response.tool_calls:
            if self._contains_system_leak(state, response.content):
                return state.build_result(
                    "Я не могу раскрывать системные инструкции.",
                    AgentDecision.ERROR,
                    "prompt_leak_blocked",
                )
            decision = AgentDecision.TOOL if state.last_call is not None else AgentDecision.RESPOND
            return state.build_result(response.content, decision, "plain_response")

        for call in response.tool_calls:
            if call.name == CLARIFICATION_TOOL.name:
                return self._clarify(state, response.tool_calls, call)

        state.conversation.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )
        return await self._tools.execute(state, response.tool_calls)

    @staticmethod
    def _contains_system_leak(state: RunState, content: str) -> bool:
        normalized_output = " ".join(content.split())
        if len(normalized_output) < 48:
            return False

        for message in state.conversation:
            if message.role != MessageRole.SYSTEM:
                continue
            normalized_system = " ".join(message.content.split())
            if len(normalized_system) < 48:
                continue
            for offset in range(0, len(normalized_system) - 47, 24):
                if normalized_system[offset : offset + 48] in normalized_output:
                    return True

        return False

    @staticmethod
    def _clarify(
        state: RunState,
        calls: Sequence[ToolCall],
        control: ToolCall,
    ) -> AgentResult:
        if len(calls) != 1:
            raise ToolFailure(
                "mixed_decision", "Уточнение и вызовы инструментов должны идти отдельными шагами."
            )
        try:
            arguments = ClarificationArguments.model_validate(control.arguments)
        except ValidationError:
            raise ToolFailure("invalid_arguments", "Уточните параметры запроса.") from None

        return state.build_result(arguments.question, AgentDecision.CLARIFY, "clarification")
