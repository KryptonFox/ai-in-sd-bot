import asyncio
import json
import logging
import time
from typing import Any, Self

import httpx
import httpx2
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.core.context import get_request_id
from app.core.exceptions import (
    LLM_ERROR_MESSAGES,
    LLMEmptyResponseError,
    LLMError,
    LLMErrorCode,
    LLMRefusalError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.core.schemas.llm import LLMRequest, LLMResponse, ToolCall
from app.core.schemas.message import MessageRole
from app.llm.base import BaseLLMClient
from app.llm.context_length import (
    DEFAULT_CONTEXT_LENGTH as DEFAULT_CONTEXT_LENGTH,
)
from app.llm.context_length import (
    ContextLengthResolver as ContextLengthResolver,
)
from app.llm.diagnostics import api_status_error_details, generation_error_details

logger = logging.getLogger("app.llm")

HTTP_ERROR_CODES: dict[int, LLMErrorCode] = {
    400: LLMErrorCode.INVALID_REQUEST,
    401: LLMErrorCode.AUTHENTICATION,
    403: LLMErrorCode.PERMISSION,
    404: LLMErrorCode.NOT_FOUND,
    408: LLMErrorCode.TIMEOUT,
    413: LLMErrorCode.INVALID_REQUEST,
    422: LLMErrorCode.INVALID_REQUEST,
    429: LLMErrorCode.RATE_LIMIT,
}


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


class OpenAIClient(BaseLLMClient):
    def __init__(
        self,
        settings: Settings,
        client: AsyncOpenAI | None = None,
        *,
        metadata_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = (
            client.with_options(timeout=settings.llm_timeout, max_retries=0)
            if client is not None
            else AsyncOpenAI(
                api_key=settings.llm_api_key or "not-configured",
                organization="",
                project="",
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=0,
            )
        )
        self._context = ContextLengthResolver(settings, metadata_client)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            if self._owns_client:
                await self._client.close()
        finally:
            await self._context.aclose()

    async def get_context_length(self, model: str | None = None) -> int:
        return await self._context.get(model or self._settings.llm_model)

    @staticmethod
    def _payload(request: LLMRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for msg in request.messages:
            item: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
            if msg.tool_calls:
                if msg.role != MessageRole.ASSISTANT:
                    raise ValueError("tool_calls require assistant")
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments, ensure_ascii=False, allow_nan=False
                            ),
                        },
                    }
                    for call in msg.tool_calls
                ]
            if msg.role == MessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ValueError("tool result requires tool_call_id")
                item["tool_call_id"] = msg.tool_call_id
            messages.append(item)
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": tool.model_dump(exclude_none=True)}
                for tool in request.tools
            ]
        return payload

    @staticmethod
    def _response(data: ChatCompletion) -> LLMResponse:
        data = ChatCompletion.model_validate(data.model_dump(warnings=False))
        if not data.choices:
            raise LLMEmptyResponseError("Модель вернула пустой ответ.")
        choice = data.choices[0]
        message = choice.message
        if message.refusal or choice.finish_reason == "content_filter":
            raise LLMRefusalError("Модель отказалась выполнять запрос.")
        if choice.finish_reason not in {"stop", "length", "tool_calls"}:
            raise ValueError("unsupported_finish_reason")
        content = message.content or ""
        calls = []
        for call in message.tool_calls or []:
            if call.type != "function":
                raise ValueError("unsupported_tool_call")
            arguments = json.loads(
                call.function.arguments,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
        if choice.finish_reason == "tool_calls" and not calls:
            raise ValueError("missing_tool_calls")
        if not content.strip() and not calls:
            if choice.finish_reason == "length":
                raise LLMServiceError(
                    "Лимит генерации исчерпан до появления публичного ответа.",
                    code=LLMErrorCode.GENERATION_LIMIT,
                )
            raise LLMEmptyResponseError("Модель вернула пустой ответ.")
        usage = data.usage
        return LLMResponse(
            content=content,
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            tool_calls=calls,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        req_id = get_request_id() or "local"
        model = request.model or self._settings.llm_model
        started = time.monotonic()
        code = "ok"
        logger.info(
            "[%s] llm_start model=%r max_tokens=%s streaming=%s",
            req_id,
            model,
            request.max_tokens,
            self._settings.llm_streaming,
        )

        try:
            result = await self._generate(request, model)
            logger.info(
                "[%s] llm_usage model=%r prompt=%s completion=%s total=%s finish=%s",
                req_id,
                model,
                result.prompt_tokens,
                result.completion_tokens,
                result.total_tokens,
                result.finish_reason,
            )
            return result
        except LLMError as error:
            code = f"http_{error.status_code}" if error.status_code else error.code
            logger.warning(
                "[%s] llm_failed model=%r code=%s status=%s reason=%s details=%s",
                req_id,
                model,
                error.code,
                error.status_code,
                error.user_message,
                generation_error_details(error),
            )
            raise
        except asyncio.CancelledError:
            code = "cancelled"
            raise
        finally:
            logger.info(
                "[%s] llm_complete model=%r duration=%.3f code=%s",
                req_id,
                model,
                time.monotonic() - started,
                code,
            )

    async def _generate(self, request: LLMRequest, model: str) -> LLMResponse:
        if not self._settings.llm_api_key:
            raise LLMServiceError(
                "API-ключ модели не настроен. Укажите LLM_API_KEY в файле .env.",
                code=LLMErrorCode.NOT_CONFIGURED,
            )

        try:
            async with asyncio.timeout(self._settings.llm_timeout):
                payload = self._payload(request)
                if self._settings.llm_streaming:
                    return await self._stream_response(payload, model)
                data = await self._client.chat.completions.create(model=model, **payload)
                return self._response(data)
        except (APITimeoutError, TimeoutError, httpx2.TimeoutException):
            raise LLMTimeoutError("Превышено время ожидания ответа языковой модели.") from None
        except APIStatusError as exc:
            error_code = HTTP_ERROR_CODES.get(exc.status_code, LLMErrorCode.UNAVAILABLE)
            raise LLMServiceError(
                LLM_ERROR_MESSAGES[error_code],
                code=error_code,
                status_code=exc.status_code,
                diagnostic_details=api_status_error_details(exc),
            ) from None
        except (APIConnectionError, httpx2.HTTPError):
            raise LLMServiceError(
                "Не удалось соединиться с сервисом языковой модели.",
                code=LLMErrorCode.CONNECTION,
            ) from None
        except (APIError, ValueError, TypeError, AttributeError):
            raise LLMServiceError(
                "Не удалось получить корректный ответ языковой модели.",
                code=LLMErrorCode.INVALID_RESPONSE,
            ) from None

    async def _stream_response(self, payload: dict[str, Any], model: str) -> LLMResponse:
        async with self._client.chat.completions.stream(
            model=model, stream_options={"include_usage": True}, **payload
        ) as stream:
            await stream.until_done()
            try:
                completion = stream.current_completion_snapshot
            except AssertionError:
                raise LLMEmptyResponseError("Модель вернула пустой поток ответа.") from None
            return self._response(completion)
