import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import APIStatusError, AsyncOpenAI

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
from app.llm.diagnostics import api_status_error_details

SETTINGS = Settings(
    bot_token="unused",
    postgres_password="unused",
    llm_api_key="secret-key",
    llm_base_url="https://provider.test/v1",
    llm_model="configured-model",
    llm_streaming=False,
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


async def test_api_400_logs_specific_safe_parameter_diagnosis(adapter, caplog):
    client, _ = adapter(
        lambda _: httpx2.Response(
            400,
            json={
                "error": {
                    "message": "Unsupported value: temperature is not supported for this model.",
                    "type": "invalid_request_error",
                    "code": "unsupported_parameter",
                    "param": "temperature",
                }
            },
        )
    )

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request())

    assert "model='configured-model'" in caplog.text
    assert "provider_code=unsupported_parameter" in caplog.text
    assert "provider_type=invalid_request_error" in caplog.text
    assert "provider_param=temperature" in caplog.text
    assert "provider_reason_code=unsupported_parameter_for_temperature" in caplog.text
    assert (
        "provider_reason=API отклонил temperature: параметр или значение не поддерживается моделью"
        in caplog.text
    )


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": 42, "code": ["unsupported_parameter"], "param": {}}},
        {"error": {"message": "PRIVATE PROMPT api-key=sk-secret-value", "code": None}},
    ],
)
async def test_incomplete_provider_error_uses_safe_fallback(adapter, caplog, body):
    client, _ = adapter(lambda _: httpx2.Response(400, json=body))

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request())

    assert "provider_reason_code=unclassified_api_error" in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text
    assert "api-key" not in caplog.text
    assert "sk-secret-value" not in caplog.text
    assert "unsupported_parameter']" not in caplog.text


@pytest.mark.parametrize(
    ("message", "reason_code", "reason"),
    [
        (
            "No endpoints found for this model",
            "no_available_endpoints",
            "нет доступных точек подключения",
        ),
        (
            "maximum context length exceeded",
            "context_length_exceeded",
            "превышен лимит контекста",
        ),
        (
            "Unsupported parameter: temperature",
            "unsupported_parameter_for_temperature",
            "API отклонил temperature",
        ),
        (
            "temperature does not support this value",
            "unsupported_parameter_for_temperature",
            "API отклонил temperature",
        ),
        (
            "temperature is not supported for this model",
            "unsupported_parameter_for_temperature",
            "API отклонил temperature",
        ),
        (
            "You exceeded your current quota",
            "insufficient_quota",
            "API сообщил об исчерпанной квоте",
        ),
    ],
)
async def test_message_only_provider_errors_have_safe_specific_reasons(
    adapter, caplog, message, reason_code, reason
):
    client, _ = adapter(lambda _: httpx2.Response(400, json={"message": message}))

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request())

    assert f"provider_reason_code={reason_code}" in caplog.text
    assert reason in caplog.text
    assert message not in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text


def test_sdk_message_only_error_is_classified_without_logging_raw_message():
    request = httpx2.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx2.Response(400, request=request)
    error = APIStatusError(
        "Unsupported parameter: temperature",
        response=response,
        body=None,
    )

    details = api_status_error_details(error)

    assert "provider_reason_code=unsupported_parameter_for_temperature" in details
    assert (
        "provider_reason=API отклонил temperature: параметр или значение не поддерживается моделью"
        in details
    )
    assert "Unsupported parameter: temperature" not in details


@pytest.mark.parametrize(
    ("status", "reason_code", "reason"),
    [
        (401, "authentication_rejected", "API отклонил учётные данные"),
        (403, "permission_denied", "API запретил доступ к ресурсу"),
        (404, "resource_not_found", "API не нашёл адрес или модель"),
        (429, "rate_limit_or_quota", "API ограничил запросы или квоту"),
        (500, "provider_server_error", "API вернул внутреннюю ошибку сервера"),
        (503, "provider_server_error", "API вернул внутреннюю ошибку сервера"),
    ],
)
async def test_http_status_has_safe_reason_without_provider_code(
    adapter, caplog, status, reason_code, reason
):
    client, _ = adapter(lambda _: httpx2.Response(status, json={"message": "failure"}))

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError):
        await client.generate(request())

    assert f"provider_reason_code={reason_code}" in caplog.text
    assert reason in caplog.text
    assert "failure" not in caplog.text


async def test_invalid_response_logs_field_and_validation_problem(adapter, caplog):
    client, _ = adapter(lambda _: httpx2.Response(200, json=completion(123)))

    with caplog.at_level("INFO", logger="app.llm"), pytest.raises(LLMServiceError) as raised:
        await client.generate(request())

    assert "field=choices.0.message.content problem=string_type" in caplog.text
    assert "code=llm_invalid_response" in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text
    assert raised.value.diagnostic_details is not None
    assert "stage=response_validation" in raised.value.diagnostic_details
    assert "field=choices.0.message.content problem=string_type" in raised.value.diagnostic_details
    assert "Поле ответа должно быть строкой" in raised.value.diagnostic_details


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
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    assert "code=llm_invalid_response" in caplog.text
    assert raised.value.diagnostic_details is not None
    assert "stage=tool_arguments" in raised.value.diagnostic_details
    assert (
        "field=choices.0.message.tool_calls.0.function.arguments" in raised.value.diagnostic_details
    )
    if arguments == "broken":
        assert "invalid_json_line=1 column=1" in caplog.text
    elif arguments == '{"x": NaN}':
        assert "problem=invalid_json_constant" in caplog.text
    else:
        assert "problem=tool arguments must be an object" in caplog.text


async def test_stream_provider_failure_keeps_api_diagnosis_in_exception(adapter, caplog):
    body = (
        'data: {"error":{"code":"server_error",'
        '"message":"PRIVATE PROVIDER ERROR secret-key PRIVATE PROMPT"}}\n\n'
    )
    client, _ = adapter(
        lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    assert raised.value.code == LLMErrorCode.UNAVAILABLE
    details = raised.value.diagnostic_details or ""
    assert "stage=stream_decode" in details
    assert "provider_code=server_error" in details
    assert "provider_reason_code=provider_server_error" in details
    for private in ("PRIVATE PROVIDER ERROR", "secret-key", "PRIVATE PROMPT"):
        assert private not in details + caplog.text


async def test_incomplete_stream_identifies_missing_finish_reason(adapter, caplog):
    client, _ = adapter(
        lambda _: stream_response([stream_event({"role": "assistant", "content": "PRIVATE"})]),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    details = raised.value.diagnostic_details or ""
    assert "stage=response_validation" in details
    assert "field=choices.0.finish_reason" in details
    assert "Поле ответа содержит неподдерживаемое значение" in details
    assert "PRIVATE" not in details + caplog.text


async def test_unsupported_legacy_finish_reason_has_actual_and_expected_values(adapter):
    client, _ = adapter(lambda _: httpx2.Response(200, json=completion(reason="function_call")))
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    details = raised.value.diagnostic_details or ""
    assert "field=choices.0.finish_reason" in details
    assert "received=function_call" in details
    assert "expected=stop|length|tool_calls|content_filter" in details


async def test_internal_sdk_is_closed_on_exit_and_exception(monkeypatch):
    sdk = AsyncMock()
    monkeypatch.setattr("app.llm.client.AsyncOpenAI", lambda **kwargs: sdk)
    with pytest.raises(RuntimeError):
        async with OpenAIClient(SETTINGS):
            raise RuntimeError
    sdk.close.assert_awaited_once()


def stream_event(
    delta: dict,
    finish: str | None = None,
    usage: dict | None = None,
    *,
    empty_choices: bool = False,
) -> str:
    body = {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "configured-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    if empty_choices:
        body["choices"] = []
    return "data: " + json.dumps(body) + "\n\n"


def stream_response(events: list[str]) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=": OPENROUTER PROCESSING\n\n" + "".join(events) + "data: [DONE]\n\n",
    )


@pytest.mark.parametrize("empty_usage_choices", [False, True])
async def test_stream_assembles_answer_and_usage_without_exposing_reasoning(
    adapter, caplog, empty_usage_choices
):
    def handler(req):
        payload = json.loads(req.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        return stream_response(
            [
                stream_event({"role": "assistant", "reasoning": "PRIVATE REASONING"}),
                stream_event({"content": "Публичный "}),
                stream_event({"content": "ответ"}, "stop"),
                stream_event(
                    {},
                    "stop",
                    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    empty_choices=empty_usage_choices,
                ),
            ]
        )

    client, _ = adapter(handler, settings=replace(SETTINGS, llm_streaming=True))
    result = await client.generate(request())
    assert result.content == "Публичный ответ" and result.finish_reason == "stop"
    assert result.total_tokens == 15
    assert "PRIVATE REASONING" not in result.model_dump_json() + caplog.text


async def test_stream_accepts_repeated_assistant_role_in_usage_chunk(adapter):
    client, _ = adapter(
        lambda _: stream_response(
            [
                stream_event({"role": "assistant", "content": "Ответ"}),
                stream_event({}, "stop"),
                stream_event(
                    {"role": "assistant", "content": ""},
                    "stop",
                    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ),
            ]
        ),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    result = await client.generate(request())
    assert result.content == "Ответ" and result.total_tokens == 15


async def test_stream_infers_assistant_when_role_is_omitted(adapter):
    client, _ = adapter(
        lambda _: stream_response([stream_event({"content": "Ответ"}, "stop")]),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    result = await client.generate(request())
    assert result.content == "Ответ" and result.finish_reason == "stop"


@pytest.mark.parametrize("role", ["user", "system", "tool", "assistantuser", "PRIVATE PROMPT"])
async def test_stream_still_rejects_conflicting_or_unknown_role(adapter, role, caplog):
    client, _ = adapter(
        lambda _: stream_response([stream_event({"role": role, "content": "Answer"}, "stop")]),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    details = raised.value.diagnostic_details or ""
    assert "field=choices.0.message.role" in details
    expected = role if role in {"user", "system", "tool"} else "unrecognized_string"
    assert f"received_role={expected}" in details
    assert "expected_role=assistant" in details
    assert "PRIVATE PROMPT" not in details + caplog.text


async def test_nonstream_response_keeps_strict_role_validation(adapter):
    client, _ = adapter(lambda _: httpx2.Response(200, json=completion(role="assistantassistant")))
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    assert "field=choices.0.message.role" in (raised.value.diagnostic_details or "")


async def test_reasoning_only_stream_reports_generation_limit(adapter):
    client, _ = adapter(
        lambda _: stream_response(
            [stream_event({"role": "assistant", "reasoning": "PRIVATE REASONING"}, "length")]
        ),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    assert raised.value.code == LLMErrorCode.GENERATION_LIMIT


async def test_stream_reassembles_tool_argument_fragments_before_validation(adapter):
    events = [
        stream_event(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    }
                ],
            }
        ),
        stream_event(
            {
                "role": "assistant",
                "tool_calls": [{"index": 0, "function": {"arguments": '"Казань"}'}}],
            },
            "tool_calls",
        ),
    ]
    client, _ = adapter(
        lambda _: stream_response(events), settings=replace(SETTINGS, llm_streaming=True)
    )
    result = await client.generate(request(tools=[ToolDefinition(name="weather")]))
    assert result.tool_calls == [
        ToolCall(id="call-1", name="weather", arguments={"city": "Казань"})
    ]


async def test_stream_preserves_truncated_text_and_rejects_incomplete_tool_arguments(adapter):
    settings = replace(SETTINGS, llm_streaming=True)
    client, _ = adapter(
        lambda _: stream_response(
            [stream_event({"role": "assistant", "content": "Начало"}, "length")]
        ),
        settings=settings,
    )
    result = await client.generate(request())
    assert result.content == "Начало" and result.finish_reason == "length"
    broken, _ = adapter(
        lambda _: stream_response(
            [
                stream_event(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "weather", "arguments": '{"city":'},
                            }
                        ],
                    },
                    "length",
                )
            ]
        ),
        settings=settings,
    )
    with pytest.raises(LLMServiceError):
        await broken.generate(request(tools=[ToolDefinition(name="weather")]))


async def test_stream_disconnection_never_returns_partial_answer(adapter, caplog):
    class BrokenStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield stream_event({"role": "assistant", "content": "Partial"}).encode()
            raise httpx2.RemoteProtocolError("PRIVATE TRANSPORT DETAIL")

    client, _ = adapter(
        lambda _: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
        ),
        settings=replace(SETTINGS, llm_streaming=True),
    )
    with pytest.raises(LLMServiceError) as raised:
        await client.generate(request())
    assert raised.value.code == LLMErrorCode.CONNECTION
    assert "PRIVATE TRANSPORT DETAIL" not in caplog.text


async def test_stream_deadline_and_cancellation_close_transport(adapter):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield stream_event({"role": "assistant", "content": "Partial"}).encode()
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    client, _ = adapter(
        lambda _: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SlowStream()
        ),
        settings=replace(SETTINGS, llm_streaming=True, llm_timeout=0.03),
    )
    with pytest.raises(LLMTimeoutError):
        await client.generate(request())
    assert closed.is_set()
    closed.clear()
    entered.clear()
    task = asyncio.create_task(client.generate(request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_stream_error_event_and_missing_finish_are_failures(adapter):
    settings = replace(SETTINGS, llm_streaming=True)
    for body in (
        'data: {"error":{"message":"PRIVATE PROVIDER ERROR","code":"server_error"}}\n\n',
        stream_event({"role": "assistant", "content": "Partial"}),
        "data: [DONE]\n\n",
    ):
        client, _ = adapter(
            lambda _, body=body: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            ),
            settings=settings,
        )
        with pytest.raises((LLMServiceError, LLMEmptyResponseError)):
            await client.generate(request())
