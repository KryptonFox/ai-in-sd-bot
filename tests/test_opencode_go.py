import json
import re
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI

from app.config import ConfigError, Settings
from app.core.schemas.llm import LLMRequest
from app.core.schemas.message import ChatMessage, MessageRole
from app.llm.client import (
    OPENCODE_SESSION_HEADER,
    OPENCODE_USER_AGENT,
    OpenAIClient,
)

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
SESSION_PATTERN = re.compile(r"ses_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SETTINGS = Settings(
    bot_token="unused",
    postgres_password="unused",
    llm_api_key="secret-key",
    llm_base_url="https://provider.test/v1",
    llm_model="configured-model",
    llm_streaming=False,
)


def completion() -> dict:
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "public"},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def stream_response() -> httpx2.Response:
    event = {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "configured-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "public"},
                "finish_reason": "stop",
            }
        ],
    }
    body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


def request() -> LLMRequest:
    return LLMRequest(
        messages=[ChatMessage(role=MessageRole.USER, content="PRIVATE PROMPT")],
    )


def load_settings(tmp_path: Path, **environment: str) -> Settings:
    return Settings.load(
        tmp_path / "missing.env",
        environ={
            "BOT_TOKEN": TOKEN,
            "POSTGRES_PASSWORD": "unused",
            **environment,
        },
    )


@pytest.fixture
async def adapter():
    clients: list[AsyncOpenAI] = []

    def make(handler, *, settings: Settings = SETTINGS) -> OpenAIClient:
        sdk = AsyncOpenAI(
            api_key=settings.llm_api_key or "not-configured",
            base_url=settings.llm_base_url,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            max_retries=0,
        )
        clients.append(sdk)
        return OpenAIClient(settings, client=sdk)

    yield make

    for sdk in clients:
        await sdk.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", False),
        ("1", True),
        ("false", False),
        ("true", True),
        ("FALSE", False),
        ("TRUE", True),
    ],
)
def test_opencode_session_flag_uses_standard_boolean_values(
    tmp_path: Path,
    value: str,
    expected: bool,
) -> None:
    settings = load_settings(tmp_path, LLM_OPENCODE_SESSION=value)

    assert settings.llm_opencode_session is expected


def test_opencode_session_flag_is_disabled_by_default(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path,
        LLM_API_KEY="original-key",
        LLM_BASE_URL="https://original.test/v1",
        LLM_MODEL="original-model",
    )

    assert settings.llm_opencode_session is False
    assert settings.llm_api_key == "original-key"
    assert settings.llm_base_url == "https://original.test/v1"
    assert settings.llm_model == "original-model"


def test_existing_settings_constructor_keeps_flag_off() -> None:
    assert SETTINGS.llm_opencode_session is False


def test_invalid_opencode_session_flag_is_a_safe_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_settings(
            tmp_path,
            LLM_API_KEY="private-key",
            LLM_OPENCODE_SESSION="yes",
        )

    assert "LLM_OPENCODE_SESSION: укажите true или false." in str(error.value)
    assert "private-key" not in str(error.value)


async def test_disabled_flag_keeps_existing_nonstream_request(adapter) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=completion())

    client = adapter(handler)
    await client.generate(request())

    assert seen[0].headers.get(OPENCODE_SESSION_HEADER) is None
    assert OPENCODE_USER_AGENT not in seen[0].headers.get("user-agent", "")

    payload = json.loads(seen[0].content)
    assert payload["model"] == "configured-model"
    assert "thinking" not in payload
    assert "reasoning" not in payload


async def test_disabled_flag_keeps_existing_stream_request(adapter) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return stream_response()

    client = adapter(handler, settings=replace(SETTINGS, llm_streaming=True))
    await client.generate(request())

    assert seen[0].headers.get(OPENCODE_SESSION_HEADER) is None
    assert OPENCODE_USER_AGENT not in seen[0].headers.get("user-agent", "")

    payload = json.loads(seen[0].content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "thinking" not in payload
    assert "reasoning" not in payload


async def test_enabled_flag_adds_stable_headers_to_nonstream_requests(adapter) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=completion())

    settings = replace(
        SETTINGS,
        llm_model="deepseek-v4.1-flash",
        llm_opencode_session=True,
    )
    client = adapter(handler, settings=settings)

    await client.generate(request())
    await client.generate(request())

    first_session = seen[0].headers.get(OPENCODE_SESSION_HEADER)
    assert first_session is not None
    assert SESSION_PATTERN.fullmatch(first_session)
    assert seen[0].headers.get("user-agent") == OPENCODE_USER_AGENT
    assert seen[1].headers.get(OPENCODE_SESSION_HEADER) == first_session
    assert seen[1].headers.get("user-agent") == OPENCODE_USER_AGENT

    payload = json.loads(seen[0].content)
    assert payload["model"] == "deepseek-v4.1-flash"
    assert "thinking" not in payload
    assert "reasoning" not in payload


async def test_enabled_flag_adds_stable_headers_to_stream_requests(adapter) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return stream_response()

    settings = replace(SETTINGS, llm_streaming=True, llm_opencode_session=True)
    client = adapter(handler, settings=settings)

    await client.generate(request())
    await client.generate(request())

    first_session = seen[0].headers.get(OPENCODE_SESSION_HEADER)
    assert first_session is not None
    assert SESSION_PATTERN.fullmatch(first_session)
    assert seen[0].headers.get("user-agent") == OPENCODE_USER_AGENT
    assert seen[1].headers.get(OPENCODE_SESSION_HEADER) == first_session
    assert seen[1].headers.get("user-agent") == OPENCODE_USER_AGENT

    payload = json.loads(seen[0].content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "thinking" not in payload
    assert "reasoning" not in payload


async def test_each_client_gets_its_own_opaque_session(adapter) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=completion())

    settings = replace(SETTINGS, llm_opencode_session=True)
    first_client = adapter(handler, settings=settings)
    second_client = adapter(handler, settings=settings)

    await first_client.generate(request())
    await second_client.generate(request())

    first_session = seen[0].headers.get(OPENCODE_SESSION_HEADER)
    second_session = seen[1].headers.get(OPENCODE_SESSION_HEADER)
    assert first_session is not None
    assert second_session is not None
    assert first_session != second_session
