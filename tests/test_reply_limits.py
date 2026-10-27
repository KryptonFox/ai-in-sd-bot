from unittest.mock import AsyncMock, Mock

from mcp import types

from app.config import Settings
from app.core.budget import ContextBudget
from app.core.formatters import _split_messages
from app.core.schemas.agent import EventContext
from app.core.schemas.llm import LLMResponse
from app.core.schemas.message import ToolCall
from app.mcp.client import MCPClient
from app.mcp_server.server import tool_catalog
from app.services.agent_service import AgentService
from app.services.context_builder import ContextBuilder
from app.services.tool_execution import ToolExecutionService


async def test_output_budget_preserves_large_configured_limit() -> None:
    llm = Mock(get_context_length=AsyncMock(return_value=40960))
    builder = ContextBuilder(
        Mock(), Settings(bot_token="unused", postgres_password="unused", llm_max_tokens=16384), llm
    )
    budget = await builder.budget()
    assert budget.output_tokens == 16384


async def test_large_structured_answer_is_preserved_and_split_for_telegram() -> None:
    text = "Длинный ответ с формулой и пояснением. " * 1000
    llm = Mock(
        generate=AsyncMock(
            return_value=LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_response",
                        arguments={"action": "respond", "text": text},
                    )
                ],
            )
        )
    )
    result = await AgentService(llm).run(
        [], EventContext(user_id=1, event_id="long", request_id="long")
    )
    assert result.decision == "respond" and result.text == text
    chunks = _split_messages(result.text, [])
    assert len(chunks) > 1
    assert "".join(chunk for chunk, _ in chunks) == text


async def test_tool_results_shrink_output_reserve_without_discarding_answer() -> None:
    client = MCPClient(Settings(bot_token="unused", postgres_password="unused"))
    client.load_catalog(tool_catalog())
    client._command = AsyncMock(
        return_value=types.CallToolResult(
            structuredContent={
                "date": "2026-10-27",
                "lessons": [
                    {
                        "start": "09:00",
                        "end": "10:30",
                        "title": "Дискретная математика " * 200,
                        "location": "Аудитория 1",
                    }
                ],
            },
            content=[],
        )
    )
    llm = Mock(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="schedule", name="get_schedule", arguments={"date": "2026-10-27"}
                        )
                    ],
                ),
                LLMResponse(content="Завтра первая пара в 09:00."),
            ]
        )
    )
    result = await AgentService(llm, ToolExecutionService(client)).run(
        [],
        EventContext(user_id=1, event_id="budget", request_id="budget"),
        budget=ContextBudget(24000, 16384),
    )
    assert result.decision == "tool"
    first, second = [call.args[0].max_tokens for call in llm.generate.await_args_list]
    assert 64 <= second < first <= 16384
