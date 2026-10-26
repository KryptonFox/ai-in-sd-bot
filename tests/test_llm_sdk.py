import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.core.exceptions import (
    LLMEmptyResponseError,
    LLMErrorCode,
    LLMRefusalError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.core.schemas.llm import LLMRequest, ToolCall, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole
from app.llm.client import OpenAIClient

SETTINGS = Settings(
    bot_token="unused",
    postgres_password="unused",
    llm_api_key="secret-key",
    llm_base_url="https://provider.test/v1",
    llm_model="configured-model",
)


def completion(content="public", *, reason="stop", **message):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": reason,
                "message": {"role": "assistant", "content": content, **message},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def request(**kwargs):
    return LLMRequest(
        messages=[ChatMessage(role=MessageRole.USER, content="PRIVATE PROMPT")], **kwargs
    )


@pytest.fixture
async def adapter():
    clients = []

    def make(handler, *, settings=SETTINGS):
        sdk = AsyncOpenAI(
            api_key=settings.llm_api_key or "not-configured",
            base_url=settings.llm_base_url,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            max_retries=2,
            _strict_response_validation=True,
        )
        clients.append(sdk)
        return OpenAIClient(settings, client=sdk), sdk

    yield make
    for sdk in clients:
        await sdk.close()


async def test_sdk_payload_usage_and_reuse(adapter):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx2.Response(200, json=completion("  public\n", reason="length"))

    client, sdk = adapter(handler)
    result = await client.generate(request(temperature=0.3, max_tokens=123))
    await client.generate(request(model="override"))
    payload = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://provider.test/v1/chat/completions"
    assert payload["model"] == "configured-model"
    assert payload["temperature"] == 0.3 and payload["max_tokens"] == 123
    assert json.loads(seen[1].content)["model"] == "override"
    assert result.content == "  public\n" and result.finish_reason == "length"
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (11, 7, 18)
    await client.aclose()
    assert not sdk.is_closed()


@pytest.mark.parametrize(
    "body,error",
    [
        ({"choices": []}, LLMEmptyResponseError),
        (completion(None, reasoning="SECRET REASONING"), LLMEmptyResponseError),
        (completion(""), LLMEmptyResponseError),
        (completion(None, refusal="no"), LLMRefusalError),
        (completion(None, reason="content_filter"), LLMRefusalError),
        (completion(None, reason="length"), LLMServiceError),
        ({"error": "unexpected"}, LLMServiceError),
        (completion(123), LLMServiceError),
    ],
)
async def test_response_boundaries(adapter, body, error):
    if body == {"choices": []}:
        body = {**completion(), **body}
    client, _ = adapter(lambda _: httpx2.Response(200, json=body))
    with pytest.raises(error):
        await client.generate(request())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, LLMErrorCode.INVALID_REQUEST),
        (401, LLMErrorCode.AUTHENTICATION),
        (403, LLMErrorCode.PERMISSION),
        (404, LLMErrorCode.NOT_FOUND),
        (408, LLMErrorCode.TIMEOUT),
        (422, LLMErrorCode.INVALID_REQUEST),
        (429, LLMErrorCode.RATE_LIMIT),
        (500, LLMErrorCode.UNAVAILABLE),
        (503, LLMErrorCode.UNAVAILABLE),
    ],
)
async def test_http_errors_are_safe_and_never_retried(adapter, status, code, caplog):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx2.Response(
            status,
            json={
                "error": {
                    "message": "secret-key PRIVATE PROMPT",
                    "code": "secret-key",
                    "param": "PRIVATE PROMPT",
                }
            },
        )

    client, _ = adapter(handler)
    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError) as error:
        await client.generate(request())
    assert len(calls) == 1
    assert "PRIVATE PROMPT" not in caplog.text + str(error.value)
    assert "secret-key" not in caplog.text + str(error.value)
    assert f"http_{status}" in caplog.text
    assert error.value.code == code
    assert error.value.status_code == status
    assert "model='configured-model'" in caplog.text
    assert f"code={code}" in caplog.text
    assert f"status={status}" in caplog.text
    assert "exception=" in caplog.text


async def test_provider_quota_code_and_model_override_are_logged(adapter, caplog):
    client, _ = adapter(
        lambda _: httpx2.Response(
            429,
            json={"error": {"message": "PRIVATE DETAIL", "code": "insufficient_quota"}},
        )
    )

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request(model="override-model"))

    assert "model='override-model'" in caplog.text
    assert "provider_code=insufficient_quota" in caplog.text
    assert "PRIVATE DETAIL" not in caplog.text


async def test_invalid_response_logs_field_and_validation_problem(adapter, caplog):
    client, _ = adapter(lambda _: httpx2.Response(200, json=completion(123)))

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request())

    assert "field=choices.0.message.content problem=string_type" in caplog.text
    assert "code=llm_invalid_response" in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text


async def test_missing_api_key_is_reported_without_network_request(adapter):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx2.Response(200, json=completion())

    client, _ = adapter(handler, settings=replace(SETTINGS, llm_api_key=""))

    with pytest.raises(LLMServiceError) as error:
        await client.generate(request())

    assert error.value.code == LLMErrorCode.NOT_CONFIGURED
    assert calls == []


async def test_generation_limit_has_its_own_reason(adapter):
    client, _ = adapter(lambda _: httpx2.Response(200, json=completion(None, reason="length")))

    with pytest.raises(LLMServiceError) as error:
        await client.generate(request())

    assert error.value.code == LLMErrorCode.GENERATION_LIMIT
    assert "лимит генерации" in error.value.user_message


@pytest.mark.parametrize(
    "transport_error,error",
    [
        (httpx2.ReadTimeout, LLMTimeoutError),
        (httpx2.ConnectError, LLMServiceError),
    ],
)
async def test_transport_errors(adapter, transport_error, error, caplog):
    def handler(req):
        raise transport_error("PRIVATE PROVIDER DETAIL", request=req)

    client, _ = adapter(handler)
    with pytest.raises(error, match="модел"):
        await client.generate(request())
    assert f"exception={transport_error.__name__}" in caplog.text
    assert "PRIVATE PROVIDER DETAIL" not in caplog.text


async def test_total_deadline_and_cancellation_stop_request(adapter):
    stopped = asyncio.Event()
    started = asyncio.Event()

    async def slow(req):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    client, _ = adapter(slow, settings=replace(SETTINGS, llm_timeout=0.03))
    with pytest.raises(LLMTimeoutError):
        await client.generate(request())
    assert stopped.is_set()
    stopped.clear()
    started.clear()
    task = asyncio.create_task(client.generate(request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_tools_roundtrip(adapter):
    seen = []
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "weather", "arguments": '{"city": "Москва"}'},
    }

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx2.Response(200, json=completion(None, reason="tool_calls", tool_calls=[call]))

    client, _ = adapter(handler)
    response = await client.generate(request(tools=[ToolDefinition(name="weather")]))
    assert response.content == ""
    assert response.tool_calls == [
        ToolCall(id="call-1", name="weather", arguments={"city": "Москва"})
    ]
    await client.generate(
        LLMRequest(
            messages=[
                ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=response.tool_calls),
                ChatMessage(role=MessageRole.TOOL, content='{"temp": 7}', tool_call_id="call-1"),
            ]
        )
    )
    assert seen[0]["tools"][0]["function"]["name"] == "weather"
    assert json.loads(seen[1]["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {
        "city": "Москва"
    }
    assert seen[1]["messages"][1]["tool_call_id"] == "call-1"


@pytest.mark.parametrize("arguments", ["broken", "[]", '"text"', "null", '{"x": NaN}'])
async def test_invalid_tool_arguments_are_service_errors(adapter, arguments, caplog):
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "weather", "arguments": arguments},
    }
    client, _ = adapter(
        lambda _: httpx2.Response(
            200, json=completion(None, reason="tool_calls", tool_calls=[call])
        )
    )
    with pytest.raises(LLMServiceError):
        await client.generate(request())
    assert "code=llm_invalid_response" in caplog.text
    if arguments == "broken":
        assert "invalid_json_line=1 column=1" in caplog.text
    elif arguments == '{"x": NaN}':
        assert "problem=invalid_json_constant" in caplog.text
    else:
        assert "problem=tool arguments must be an object" in caplog.text


async def test_internal_sdk_is_closed_on_exit_and_exception(monkeypatch):
    sdk = AsyncMock()
    monkeypatch.setattr("app.llm.client.AsyncOpenAI", lambda **kwargs: sdk)
    with pytest.raises(RuntimeError):
        async with OpenAIClient(SETTINGS):
            raise RuntimeError
    sdk.close.assert_awaited_once()
