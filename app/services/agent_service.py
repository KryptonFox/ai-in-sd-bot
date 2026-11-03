from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.budget import ContextBudget
from app.core.clock import Clock, SystemClock
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
from app.llm.base import BaseLLMClient
from app.prompts.agent import agent_rules
from app.services.tool_execution import READ_RESOURCE_FUNCTION, ToolExecutionService

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
    ) -> None:
        self._llm = llm
        self._tools = tools or ToolExecutionService()
        self._clock = clock or SystemClock()
        self._max_steps = max_llm_steps

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
        return agent_rules(local, timezone, tools_available=self._tools.available)

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
    ) -> AgentResult:
        state = RunState(
            context=context,
            conversation=list(messages),
            timezone=timezone,
            calls_used=calls_used,
        )
        self._append_instructions(state)
        budget = budget or ContextBudget(32768)

        try:
            return await self._run_steps(state, temperature, model, budget)
        except ToolFailure as error:
            decision = (
                AgentDecision.CLARIFY if error.code in CLARIFICATION_ERRORS else AgentDecision.ERROR
            )
            return state.build_result(error.message, decision, error.code)

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
        definitions = self.definitions

        for _ in range(self._max_steps):
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
            outcome = await self._handle_response(state, response)
            if isinstance(outcome, AgentResult):
                return outcome

        raise ToolFailure("step_budget", "Достигнут лимит шагов модели. Уточните запрос.")

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

            return state.build_result(
                response.content,
                AgentDecision.RESPOND,
                "truncated_response",
                truncated=True,
            )

        if not response.tool_calls:
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
