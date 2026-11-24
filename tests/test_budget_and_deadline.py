import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastmcp import Client, FastMCP

from app.core.budget import ContextBudget
from app.core.clock import FrozenClock
from app.core.schemas.agent import AgentDecision, EventContext
from app.core.schemas.llm import FinishReason, LLMRequest, LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.mcp.client import MCPClient
from app.services.agent_service import AgentService
from app.services.context_builder import ContextBuilder
from app.services.tool_execution import ToolExecutionService
from tests.mcp_catalog import tool_catalog

CONTEXT = EventContext(user_id=1, event_id="budget", request_id="budget-request")
NOW = datetime(2026, 10, 12, tzinfo=UTC)


class ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(update={"messages": list(request.messages)}))
        return next(self._responses)

    async def get_context_length(self, model: str | None = None) -> int:
        return 32768

    async def aclose(self) -> None:
        pass


class ScriptedToolClient(MCPClient):
    def __init__(self, result: dict[str, object], delay: float = 0) -> None:
        super().__init__(Client(FastMCP("budget-test")))
        self.load_catalog(tool_catalog())
        self._result = result
        self._delay = delay
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((name, arguments))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


def tool_call(identifier: str) -> ToolCall:
    return ToolCall(
        id=identifier,
        name="get_schedule",
        arguments={"date": "2026-10-12"},
    )


def test_context_fit_drops_whole_tool_call_units() -> None:
    user = ChatMessage(role=MessageRole.USER, content="Старый вопрос " * 100)
    assistant_call = ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[tool_call("old-call")],
    )
    tool_result = ChatMessage(
        role=MessageRole.TOOL,
        content="x" * 2000,
        tool_call_id="old-call",
    )
    assistant_answer = ChatMessage(role=MessageRole.ASSISTANT, content="Старый ответ " * 100)
    kept_user = ChatMessage(role=MessageRole.USER, content="Новый вопрос")
    kept_answer = ChatMessage(role=MessageRole.ASSISTANT, content="Новый ответ")
    initial = [ChatMessage(role=MessageRole.SYSTEM, content="Правила")]
    current = ChatMessage(role=MessageRole.USER, content="Продолжим")
    budget = ContextBudget(0, 512, 128)
    fixed = budget.estimate([*initial, current], [])
    kept_size = budget.estimate_message(kept_user) + budget.estimate_message(kept_answer)

    prepared = ContextBuilder._fit_context(
        initial,
        current,
        [[user, assistant_call, tool_result, assistant_answer], [kept_user, kept_answer]],
        [],
        0,
        ContextBudget(fixed + 128 + kept_size + 10, 512, 128),
    )

    assert prepared.messages == [*initial, kept_user, kept_answer, current]
    assert all(message.role != MessageRole.TOOL for message in prepared.messages)


def test_generation_reserve_is_capped_by_window_profile() -> None:
    assert ContextBudget(8192, 16384, 4096).effective_generation_reserve == 1024
    assert ContextBudget(32768, 16384, 4096).effective_generation_reserve == 4096
    assert ContextBudget(131072, 32768, 4096).effective_generation_reserve == 4096
    assert ContextBudget(512, 16384, 4096).effective_generation_reserve == 64


async def test_required_context_leaving_237_tokens_is_rejected_before_llm() -> None:
    llm = ScriptedLLM([LLMResponse(content="not reached")])
    agent = AgentService(llm)
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="x" * 50_000),
        ChatMessage(role=MessageRole.USER, content="Короткий вопрос"),
    ]
    window = ContextBudget(0, 16384, 4096).estimate(messages, agent.definitions) + 237

    result = await agent.run(
        messages,
        CONTEXT,
        budget=ContextBudget(window, 16384, 4096),
        protected_prefix=1,
    )

    assert result.decision == AgentDecision.ERROR
    assert result.reason == "context_budget"
    assert llm.requests == []


async def test_history_trimming_repeats_without_splitting_tool_groups() -> None:
    old_user = ChatMessage(role=MessageRole.USER, content="Старый вопрос " * 50)
    old_answer = ChatMessage(role=MessageRole.ASSISTANT, content="Старый ответ " * 50)
    old_user_2 = ChatMessage(role=MessageRole.USER, content="Ещё вопрос " * 50)
    old_answer_2 = ChatMessage(role=MessageRole.ASSISTANT, content="Ещё ответ " * 50)
    conversation = [
        ChatMessage(role=MessageRole.SYSTEM, content="Правила"),
        old_user,
        old_answer,
        old_user_2,
        old_answer_2,
        ChatMessage(role=MessageRole.USER, content="Покажи расписание"),
    ]
    client = ScriptedToolClient({"date": "2026-10-12", "lessons": [], "payload": "x" * 1600})
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[tool_call("one")],
            ),
            LLMResponse(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[tool_call("two")],
            ),
            LLMResponse(content="Готово"),
        ]
    )
    agent = AgentService(
        llm,
        ToolExecutionService(client),
        clock=FrozenClock(NOW),
    )
    definitions = agent.definitions
    instructions = agent.instructions(None)
    conversation_with_instructions = [
        ChatMessage(role=MessageRole.SYSTEM, content="Правила\n\n" + instructions),
        *conversation[1:],
    ]
    base_estimate = ContextBudget(0, 512, 128).estimate(
        conversation_with_instructions,
        definitions,
    )

    result = await agent.run(
        conversation,
        CONTEXT,
        budget=ContextBudget(base_estimate + 1200, 512, 128),
        protected_prefix=1,
    )

    assert result.text == "Готово"
    assert result.decision == AgentDecision.TOOL
    assert len(llm.requests) == 3
    final_messages = llm.requests[-1].messages
    final_text = [message.content for message in final_messages]
    assert all("Старый" not in text and "Ещё" not in text for text in final_text)

    tool_call_ids = [call.id for message in final_messages for call in message.tool_calls]
    tool_result_ids = [
        message.tool_call_id for message in final_messages if message.role == MessageRole.TOOL
    ]
    assert tool_call_ids == ["one", "two"]
    assert tool_result_ids == ["one", "two"]
    assert any(message.content == "Покажи расписание" for message in final_messages)


async def test_expired_deadline_blocks_rpc_before_budget_reservation() -> None:
    client = ScriptedToolClient({"date": "2026-10-12", "lessons": []})
    store = MagicMock(reserve=AsyncMock(return_value=True))
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[tool_call("late")],
            )
        ]
    )
    agent = AgentService(llm, ToolExecutionService(client, store=store))

    result = await agent.run(
        [ChatMessage(role=MessageRole.USER, content="Покажи расписание")],
        CONTEXT,
        budget=ContextBudget(32768, 16384, 4096),
        deadline_at=asyncio.get_running_loop().time() - 1,
    )

    assert result.decision == AgentDecision.ERROR
    assert result.reason == "request_deadline"
    assert client.calls == []
    store.reserve.assert_not_awaited()


async def test_late_tool_call_gets_only_remaining_request_deadline() -> None:
    client = ScriptedToolClient({"date": "2026-10-12", "lessons": []}, delay=5)
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[tool_call("late")],
            ),
            LLMResponse(content="not reached"),
        ]
    )
    agent = AgentService(llm, ToolExecutionService(client))
    deadline_at = asyncio.get_running_loop().time() + 0.05
    started = time.monotonic()

    result = await agent.run(
        [ChatMessage(role=MessageRole.USER, content="Покажи расписание")],
        CONTEXT,
        budget=ContextBudget(32768, 16384, 4096),
        deadline_at=deadline_at,
    )

    assert time.monotonic() - started < 1
    assert result.decision == AgentDecision.ERROR
    assert result.reason == "request_deadline"
    assert len(llm.requests) == 1
    assert client.calls
