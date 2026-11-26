import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import (
    DEFAULT_ROUTER_BASE_URL,
    DEFAULT_ROUTER_MODEL,
    ConfigError,
    Settings,
)
from app.core.exceptions import (
    LLMErrorCode,
    LLMRefusalError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.routing import RouteDecision, RouteTarget
from app.core.trace import OperationTrace, bind_trace, trace_step
from app.llm.client import OPENCODE_SESSION_HEADER, OPENCODE_USER_AGENT
from app.llm.decisions import (
    ROUTER_HISTORY_MAX_MESSAGES,
    ROUTER_QUESTION_NAME,
    ROUTER_REASON_EMPTY_RESPONSE,
    ROUTER_REASON_INVALID_CHOICE,
    ROUTER_REASON_INVALID_CONFIDENCE,
    ROUTER_REASON_INVALID_RESPONSE,
    ROUTER_REASON_MODEL_CHOICE,
    ROUTER_REASON_UNKNOWN_TARGET,
    DecisionsRouter,
)

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
SESSION_PATTERN = re.compile(r"ses_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OPENCODE_GO_URL = "https://opencode.ai/inference/go/openai/v1"

SETTINGS = Settings(
    bot_token="unused",
    postgres_password="unused",
    llm_api_key="llm-key",
    llm_base_url=OPENCODE_GO_URL,
    llm_model="deepseek-v4.1-flash",
    router_api_key="router-key",
    router_opencode_session=False,
)


def load_settings(tmp_path: Path, **overrides: str) -> Settings:
    environment = {
        "BOT_TOKEN": TOKEN,
        "POSTGRES_PASSWORD": "unused",
        **overrides,
    }

    return Settings.load(
        tmp_path / "missing.env",
        environ=environment,
        config_file=tmp_path / "missing.toml",
    )


def decision_body(*answers: dict[str, Any]) -> dict[str, Any]:
    return {
        "answers": list(answers),
        "model": "gpt-6-luna-decision",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_tokens_details": {
                "cache_write_tokens": 0,
                "cached_tokens": 0,
            },
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def choice_answer(
    choice: str = "math",
    *,
    confidence: float = 0.91,
    name: str | None = ROUTER_QUESTION_NAME,
) -> dict[str, Any]:
    return {
        "type": "choice",
        "name": name,
        "choice": choice,
        "confidence": confidence,
        "probabilities": [{"value": choice, "probability": confidence}],
    }


@pytest.fixture
async def router_factory():
    clients: list[AsyncOpenAI] = []

    def make(handler, *, settings: Settings = SETTINGS) -> DecisionsRouter:
        sdk = AsyncOpenAI(
            api_key=settings.effective_router_api_key or "not-configured",
            base_url=settings.router_base_url,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            max_retries=0,
        )
        clients.append(sdk)

        return DecisionsRouter(settings, client=sdk)

    yield make

    for sdk in clients:
        await sdk.close()


@pytest.mark.parametrize("target", list(RouteTarget))
def test_route_decision_accepts_every_target(target: RouteTarget) -> None:
    decision = RouteDecision.model_validate(
        {
            "target": target.value,
            "reason": "model_choice",
            "confidence": 0.5,
        }
    )

    assert decision.target is target
    assert json.loads(decision.model_dump_json())["target"] == target.value


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "unknown", "reason": "model_choice", "confidence": 0.5},
        {"target": "math", "reason": " ", "confidence": 0.5},
        {"target": "math", "reason": "model_choice", "confidence": -0.1},
        {"target": "math", "reason": "model_choice", "confidence": 1.1},
        {"target": "math", "reason": "model_choice", "confidence": float("nan")},
        {"target": "math", "reason": "model_choice", "confidence": float("inf")},
    ],
)
def test_route_decision_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RouteDecision.model_validate(payload)


def test_router_defaults_and_existing_openrouter_profile_are_preserved(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)

    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert settings.llm_model == "openai/gpt-4o-mini"
    assert settings.router_base_url == DEFAULT_ROUTER_BASE_URL
    assert settings.router_model == DEFAULT_ROUTER_MODEL
    assert settings.router_timeout == 30.0
    assert settings.router_opencode_session is False
    assert settings.router_api_key == ""
    assert settings.effective_router_api_key == ""


def test_router_uses_separate_key_before_opencode_go_fallback(tmp_path: Path) -> None:
    separate = load_settings(
        tmp_path,
        ROUTER_API_KEY="router-secret",
        LLM_API_KEY="llm-secret",
        LLM_BASE_URL=OPENCODE_GO_URL,
    )
    fallback = load_settings(
        tmp_path,
        LLM_API_KEY="go-secret",
        LLM_BASE_URL=OPENCODE_GO_URL,
    )
    openrouter = load_settings(
        tmp_path,
        LLM_API_KEY="openrouter-secret",
        LLM_BASE_URL="https://openrouter.ai/api/v1",
    )

    assert separate.router_api_key == "router-secret"
    assert separate.effective_router_api_key == "router-secret"
    assert fallback.router_api_key == ""
    assert fallback.effective_router_api_key == "go-secret"
    assert openrouter.effective_router_api_key == ""


def test_router_configuration_comes_from_toml_and_environment(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[router]
base_url = "https://router.test/v1"
model = "toml-router-model"
timeout_seconds = 42.0
opencode_session = true
""",
        encoding="utf-8",
    )
    settings = Settings.load(
        tmp_path / "missing.env",
        environ={
            "BOT_TOKEN": TOKEN,
            "POSTGRES_PASSWORD": "unused",
            "ROUTER_MODEL": "env-router-model",
        },
        config_file=config_file,
    )

    assert settings.router_base_url == "https://router.test/v1"
    assert settings.router_model == "env-router-model"
    assert settings.router_timeout == 42.0
    assert settings.router_opencode_session is True


@pytest.mark.parametrize(
    "value",
    [
        "not-a-url",
        "http://user:password@router.test/v1",
        "https://router.test/v1?token=secret",
    ],
)
def test_router_invalid_urls_are_safe_config_errors(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="ROUTER_BASE_URL") as error:
        load_settings(tmp_path, ROUTER_BASE_URL=value)

    assert "password" not in str(error.value)
    assert "secret" not in str(error.value)


def test_router_secret_is_not_accepted_from_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[router]\napi_key = "router-secret"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"\[router\]\.api_key: неизвестный параметр") as error:
        Settings.load(
            tmp_path / "missing.env",
            environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "unused"},
            config_file=config_file,
        )

    assert "router-secret" not in str(error.value)


def test_diagnostics_show_router_profile_without_secrets(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path,
        ROUTER_API_KEY="router-secret",
        LLM_API_KEY="llm-secret",
    )
    diagnostics = settings.diagnostics()

    assert diagnostics["router_base_url"] == "https://opencode.ai"
    assert diagnostics["router_model"] == DEFAULT_ROUTER_MODEL
    assert diagnostics["router_timeout_seconds"] == 30.0
    assert "router-secret" not in repr(diagnostics)
    assert "llm-secret" not in repr(diagnostics)


async def test_route_sends_one_choice_question_without_tools(router_factory) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=decision_body(choice_answer()))

    router = router_factory(handler)
    decision = await router.route(
        "Проверь решение уравнения",
        [ChatMessage(role=MessageRole.ASSISTANT, content="Предыдущий ответ")],
    )

    assert decision.target is RouteTarget.MATH
    assert decision.reason == ROUTER_REASON_MODEL_CHOICE
    assert decision.confidence == 0.91

    request = seen[0]
    payload = json.loads(request.content)
    assert str(request.url) == f"{DEFAULT_ROUTER_BASE_URL}/decisions"
    assert payload["model"] == DEFAULT_ROUTER_MODEL
    assert payload["input"] == [
        {"role": "assistant", "content": "Предыдущий ответ"},
        {"role": "user", "content": "Проверь решение уравнения"},
    ]
    assert len(payload["questions"]) == 1
    question = payload["questions"][0]
    assert question["type"] == "choice"
    assert question["name"] == ROUTER_QUESTION_NAME
    assert [choice["value"] for choice in question["choices"]] == [
        target.value for target in RouteTarget
    ]
    assert "tools" not in payload
    assert "tool_choice" not in payload


async def test_route_records_provider_usage_in_a_nested_trace_step(router_factory) -> None:
    router = router_factory(
        lambda _: httpx2.Response(200, json=decision_body(choice_answer("math"))),
    )
    trace = OperationTrace(
        user_id=1,
        request_id="usage-request",
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )

    with bind_trace(trace):
        decision = await router.route("Проверь вычисление", [])

    assert decision.target is RouteTarget.MATH
    usage_steps = [
        step for step in trace.steps if step.component == "router" and step.action == "api_request"
    ]
    assert len(usage_steps) == 1
    assert usage_steps[0].model_name == DEFAULT_ROUTER_MODEL
    assert usage_steps[0].input_tokens == 10
    assert usage_steps[0].output_tokens == 2


async def test_route_usage_stays_on_the_nested_step_not_the_parent(router_factory) -> None:
    router = router_factory(
        lambda _: httpx2.Response(200, json=decision_body(choice_answer("math"))),
    )
    trace = OperationTrace(
        user_id=1,
        request_id="nested-usage-request",
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )

    with bind_trace(trace), trace_step("router", "choice") as parent:
        await router.route("Проверь вычисление", [])

    assert parent is not None
    assert parent.input_tokens is None
    assert parent.output_tokens is None
    assert [
        (step.input_tokens, step.output_tokens)
        for step in trace.steps
        if step.component == "router" and step.action == "api_request"
    ] == [(10, 2)]


async def test_route_uses_only_a_minimal_recent_text_excerpt(router_factory) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json=decision_body(choice_answer("schedule")),
        )

    history = [
        ChatMessage(role=MessageRole.USER, content=f"history-{index}")
        for index in range(ROUTER_HISTORY_MAX_MESSAGES + 3)
    ]
    history.extend(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system instruction"),
            ChatMessage(role=MessageRole.TOOL, content="tool output", tool_call_id="call-1"),
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content="tool request",
                tool_calls=[ToolCall(id="call-1", name="get_weather")],
            ),
        ]
    )
    router = router_factory(handler)

    decision = await router.route("Что у меня завтра?", history)

    assert decision.target is RouteTarget.SCHEDULE
    contents = [item["content"] for item in json.loads(seen[0].content)["input"]]
    assert contents == [
        *(f"history-{index}" for index in range(3, ROUTER_HISTORY_MAX_MESSAGES + 3)),
        "Что у меня завтра?",
    ]


async def test_route_truncates_long_history_messages(router_factory) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=decision_body(choice_answer("knowledge")))

    router = router_factory(handler)
    await router.route(
        "Найди в материалах ответ",
        [ChatMessage(role=MessageRole.USER, content="x" * 1000)],
    )

    payload = json.loads(seen[0].content)
    assert payload["input"][0]["content"] == "x" * 600


@pytest.mark.parametrize(
    ("answers", "target", "reason"),
    [
        ([], RouteTarget.CLARIFY, ROUTER_REASON_EMPTY_RESPONSE),
        ([choice_answer("unknown")], RouteTarget.CLARIFY, ROUTER_REASON_UNKNOWN_TARGET),
        (
            [choice_answer(confidence=1.5)],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_CONFIDENCE,
        ),
        (
            [choice_answer(confidence=-0.1)],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_CONFIDENCE,
        ),
        (
            [{"type": "predicate", "name": ROUTER_QUESTION_NAME, "probability": 0.8}],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_RESPONSE,
        ),
        (
            [choice_answer(name="other_question")],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_RESPONSE,
        ),
        (
            [choice_answer(name=None)],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_RESPONSE,
        ),
        (
            [
                {
                    "type": "choice",
                    "name": ROUTER_QUESTION_NAME,
                    "choice": True,
                    "confidence": 0.9,
                    "probabilities": [{"value": True, "probability": 0.9}],
                }
            ],
            RouteTarget.CLARIFY,
            ROUTER_REASON_INVALID_CHOICE,
        ),
    ],
)
async def test_invalid_router_answers_ask_for_clarification(
    router_factory,
    answers: list[dict[str, Any]],
    target: RouteTarget,
    reason: str,
) -> None:
    router = router_factory(
        lambda _: httpx2.Response(200, json=decision_body(*answers)),
    )

    decision = await router.route("Непонятный запрос", [])

    assert decision.target is target
    assert decision.reason == reason
    assert decision.confidence == 0.0


async def test_router_refusal_is_a_typed_llm_error(router_factory) -> None:
    router = router_factory(
        lambda _: httpx2.Response(
            200,
            json=decision_body({"type": "refusal", "name": ROUTER_QUESTION_NAME}),
        )
    )

    with pytest.raises(LLMRefusalError) as error:
        await router.route("Запрос", [])

    assert error.value.code is LLMErrorCode.REFUSAL


async def test_router_timeout_is_a_typed_llm_error(router_factory) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.1)
        return httpx2.Response(200, json=decision_body(choice_answer()))

    settings = replace(SETTINGS, router_timeout=0.01)
    router = router_factory(handler, settings=settings)

    with pytest.raises(LLMTimeoutError) as error:
        await router.route("Запрос", [])

    assert error.value.code is LLMErrorCode.TIMEOUT


async def test_router_provider_error_is_safe_and_typed(router_factory, caplog) -> None:
    router = router_factory(
        lambda _: httpx2.Response(
            503,
            json={
                "error": {
                    "message": "PRIVATE PROMPT router-key",
                    "code": "server_error",
                }
            },
        )
    )

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError) as error:
        await router.route("PRIVATE PROMPT", [])

    assert error.value.code is LLMErrorCode.UNAVAILABLE
    assert error.value.status_code == 503
    assert "PRIVATE PROMPT" not in caplog.text + str(error.value)
    assert "router-key" not in caplog.text + str(error.value)


async def test_router_requires_a_key(router_factory) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json=decision_body(choice_answer()))

    settings = replace(
        SETTINGS,
        router_api_key="",
        llm_api_key="",
        llm_base_url="https://openrouter.ai/api/v1",
    )
    router = router_factory(handler, settings=settings)

    with pytest.raises(LLMServiceError) as error:
        await router.route("Запрос", [])

    assert error.value.code is LLMErrorCode.NOT_CONFIGURED
    assert calls == []


async def test_router_session_headers_are_stable_when_enabled(router_factory) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=decision_body(choice_answer()))

    router = router_factory(
        handler,
        settings=replace(SETTINGS, router_opencode_session=True),
    )
    await router.route("Первый запрос", [])
    await router.route("Второй запрос", [])

    first_session = seen[0].headers.get(OPENCODE_SESSION_HEADER)
    assert first_session is not None
    assert SESSION_PATTERN.fullmatch(first_session)
    assert seen[1].headers.get(OPENCODE_SESSION_HEADER) == first_session
    assert seen[0].headers.get("user-agent") == OPENCODE_USER_AGENT
    assert seen[1].headers.get("user-agent") == OPENCODE_USER_AGENT


async def test_router_session_headers_are_absent_when_disabled(router_factory) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=decision_body(choice_answer()))

    router = router_factory(handler)
    await router.route("Запрос", [])

    assert seen[0].headers.get(OPENCODE_SESSION_HEADER) is None
    assert OPENCODE_USER_AGENT not in seen[0].headers.get("user-agent", "")
