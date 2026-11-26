import json
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest
from fastmcp import Client
from openai import AsyncOpenAI

from app.config import Settings
from app.core.schemas.agent import AgentDecision, AgentResult
from app.core.schemas.llm import FinishReason, LLMResponse
from app.core.schemas.message import ToolCall
from app.core.trace import TraceStatus
from app.db.connection import create_pool
from app.db.repositories.trace_repo import find_trace
from app.db.repositories.turn_repo import PostgresTurnStore
from app.llm.decisions import DecisionsRouter
from app.mcp.client import MCPClient
from app.mcp.composite import CompositeToolClient
from app.modes.registry import registry
from app.services.confirmation_service import ConfirmationService
from app.services.dialog_service import DialogService
from app.services.specialists import build_specialists
from app.services.supervisor import Supervisor
from app.services.tool_execution import READ_RESOURCE_FUNCTION
from mcp_servers.student_tools.config import ServerSettings, WebSearchProviderName
from mcp_servers.student_tools.server import build_server as build_student_server
from tests.conftest import DatabaseFixture
from tests.fake_llm import ScriptedLLM

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SECRET = "integration-shared-secret-0123456789abcdef"


class _StepClock:
    def __init__(self, current: datetime) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


@pytest.mark.asyncio
async def test_specialists_end_to_end_postgres_traces_confirmations_and_web(
    database: DatabaseFixture,
) -> None:
    base_settings, _, _, _ = database
    settings = Settings(
        bot_token=base_settings.bot_token,
        postgres_password=base_settings.postgres_password,
        postgres_host=base_settings.postgres_host,
        postgres_port=base_settings.postgres_port,
        postgres_db=base_settings.postgres_db,
        postgres_user=base_settings.postgres_user,
        llm_api_key="llm-key",
        router_api_key="router-key",
        mcp_shared_secret=SECRET,
    )
    pool = await create_pool(settings)
    clock = _StepClock(NOW)

    def brave_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "MCP Specification",
                            "url": "https://modelcontextprotocol.io/specification",
                            "description": "Official MCP docs.",
                            "page_age": "2026-01-10",
                        }
                    ]
                }
            },
        )

    router_choices = iter(["schedule", "schedule", "knowledge"])

    def router_handler(request: httpx2.Request) -> httpx2.Response:
        choice = next(router_choices)
        return httpx2.Response(
            200,
            json={
                "model": "gpt-6-luna-decision",
                "answers": [
                    {
                        "type": "choice",
                        "name": "route",
                        "choice": choice,
                        "confidence": 0.94,
                        "probabilities": [{"value": choice, "probability": 0.94}],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(brave_handler))
    router_sdk = AsyncOpenAI(
        api_key="router-key",
        base_url=settings.router_base_url,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(router_handler)),
        max_retries=0,
    )

    server_settings = ServerSettings(
        postgres_password=settings.postgres_password,
        postgres_host=settings.postgres_host,
        postgres_port=settings.postgres_port,
        postgres_db=settings.postgres_db,
        postgres_user=settings.postgres_user,
        mcp_shared_secret=SECRET,
        web_search_provider=WebSearchProviderName.BRAVE,
        web_search_api_key="brave-test-key",
    )
    server = build_student_server(pool, server_settings, http, clock)
    student_client = MCPClient(Client(server), secret=SECRET)
    composite = CompositeToolClient([student_client])

    try:
        assert await composite.start()
        store = PostgresTurnStore(pool)
        confirmations = ConfirmationService(pool, student_client, clock=clock)
        remind_at = (NOW + timedelta(hours=2)).astimezone(ZoneInfo("Europe/Moscow")).isoformat()
        llm = ScriptedLLM(
            [
                LLMResponse(
                    content="",
                    finish_reason=FinishReason.TOOL_CALLS,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="clarification",
                            arguments={"question": "Во сколько напомнить?"},
                        )
                    ],
                ),
                LLMResponse(
                    content="",
                    finish_reason=FinishReason.TOOL_CALLS,
                    tool_calls=[
                        ToolCall(
                            id="c2",
                            name="add_reminder",
                            arguments={"text": "Сдать отчёт", "remind_at": remind_at},
                        )
                    ],
                ),
                LLMResponse(
                    content="",
                    finish_reason=FinishReason.TOOL_CALLS,
                    tool_calls=[
                        ToolCall(
                            id="cat1",
                            name=READ_RESOURCE_FUNCTION,
                            arguments={"uri": "materials://catalog"},
                        ),
                        ToolCall(
                            id="k1",
                            name="search_materials",
                            arguments={"query": "квантовая хромодинамика"},
                        ),
                    ],
                ),
                LLMResponse(content="Нет данных."),
                LLMResponse(
                    content="",
                    finish_reason=FinishReason.TOOL_CALLS,
                    prompt_tokens=25,
                    completion_tokens=7,
                    tool_calls=[
                        ToolCall(
                            id="k2",
                            name="search_web",
                            arguments={"query": "MCP specification"},
                        )
                    ],
                ),
                LLMResponse(
                    content="Официальная спецификация: https://modelcontextprotocol.io/specification.",
                    prompt_tokens=40,
                    completion_tokens=12,
                ),
            ]
        )
        agents = build_specialists(settings, llm, composite, store, confirmations)
        supervisor = Supervisor(
            DecisionsRouter(settings, client=router_sdk), agents, settings.router_model
        )
        dialog = DialogService(
            pool,
            settings,
            llm,
            registry,
            clock=clock,
            confirmations=confirmations,
            tool_client=composite,
            store=store,
            supervisor=supervisor,
        )
        await dialog.timezone_status(1001, "Europe/Moscow")

        clarify = await dialog.process_user_message(1001, "Напомни сдать отчёт", event_id="e1")
        assert isinstance(clarify, AgentResult)
        assert clarify.decision is AgentDecision.CLARIFY

        clock.advance(timedelta(minutes=1))
        prepared = await dialog.process_user_message(1001, "Через 2 часа", event_id="e2")
        assert isinstance(prepared, AgentResult)
        assert prepared.decision is AgentDecision.PREPARE
        assert prepared.confirmation_id is not None

        forged_confirm = await dialog.confirm_action(9999, prepared.confirmation_id)
        assert forged_confirm.decision is AgentDecision.ERROR

        confirmed = await dialog.confirm_action(1001, prepared.confirmation_id)
        assert confirmed.decision is AgentDecision.TOOL
        assert "Сдать отчёт" in confirmed.text

        clock.advance(timedelta(minutes=1))
        empty_materials = await dialog.process_user_message(
            1001, "Что в материалах про квантовую хромодинамику?", event_id="e3"
        )
        assert isinstance(empty_materials, AgentResult)
        assert empty_materials.reason == "web_search_offer"
        assert empty_materials.resource_uris == ["materials://catalog"]

        e3_trace = await find_trace(pool, 1001, clock.now())
        assert e3_trace is not None
        assert e3_trace.route == "knowledge"
        router_step = next(
            step
            for step in e3_trace.steps
            if step.component == "router" and step.action == "choice"
        )
        assert router_step.model_name == settings.router_model
        assert router_step.input_tokens is None
        assert router_step.output_tokens is None

        clock.advance(timedelta(minutes=1))
        web_answer = await dialog.process_user_message(1001, "Да", event_id="e4")
        assert isinstance(web_answer, AgentResult)
        assert web_answer.decision is AgentDecision.TOOL
        assert "https://modelcontextprotocol.io/specification" in web_answer.text

        replayed = await dialog.process_user_message(1001, "Да", event_id="e4")
        assert replayed == web_answer

        owner_trace = await find_trace(pool, 1001, clock.now())
        other_trace = await find_trace(pool, 9999, clock.now())
        assert owner_trace is not None
        assert owner_trace.id != e3_trace.id
        assert owner_trace.route == "knowledge"
        assert all(step.status is TraceStatus.OK for step in owner_trace.steps)
        assert any(
            step.component == "supervisor"
            and step.action == "offer_approval"
            and step.model_name is None
            for step in owner_trace.steps
        )
        llm_steps = [
            step
            for step in owner_trace.steps
            if step.component == "llm" and step.action == "generate"
        ]
        assert [(step.input_tokens, step.output_tokens) for step in llm_steps] == [
            (25, 7),
            (40, 12),
        ]
        assert other_trace is None
        assert json.loads(json.dumps([step.action for step in owner_trace.steps]))
    finally:
        await composite.aclose()
        await router_sdk.close()
        await http.aclose()
        await pool.close()
