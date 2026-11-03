import asyncio
import logging
import time
from typing import Any, Self

import httpx
import httpx2
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.config import Settings
from app.core.context import get_request_id
from app.core.exceptions import (
    LLM_ERROR_MESSAGES,
    LLMEmptyResponseError,
    LLMError,
    LLMErrorCode,
    LLMServiceError,
    LLMTimeoutError,
)
from app.core.schemas.llm import LLMRequest, LLMResponse
from app.llm.base import BaseLLMClient
from app.llm.codec import decode_response, decode_stream_response, encode_request
from app.llm.context_length import (
    DEFAULT_CONTEXT_LENGTH as DEFAULT_CONTEXT_LENGTH,
)
from app.llm.context_length import (
    ContextLengthResolver as ContextLengthResolver,
)
from app.llm.diagnostics import (
    api_status_error_details,
    generation_error_details,
)

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
        strict_response_validation = not settings.llm_streaming
        self._client = (
            client.with_options(
                timeout=settings.llm_timeout,
                max_retries=0,
                _extra_kwargs={"_strict_response_validation": strict_response_validation},
            )
            if client is not None
            else AsyncOpenAI(
                api_key=settings.llm_api_key or "not-configured",
                organization="",
                project="",
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=0,
                _strict_response_validation=strict_response_validation,
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

        stage = "request_payload"

        try:
            async with asyncio.timeout(self._settings.llm_timeout):
                payload = encode_request(request)
                if self._settings.llm_streaming:
                    stage = "stream_decode"
                    return await self._stream_response(payload, model)

                stage = "response_decode"
                data = await self._client.chat.completions.create(model=model, **payload)

                return decode_response(data)
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
        except APIError as error:
            code = (
                LLMErrorCode.INVALID_RESPONSE
                if isinstance(error, APIResponseValidationError)
                else LLMErrorCode.UNAVAILABLE
            )
            error_stage = (
                "response_validation" if isinstance(error, APIResponseValidationError) else stage
            )
            raise LLMServiceError(
                "Ошибка API языковой модели.",
                code=code,
                diagnostic_details=f"stage={error_stage}; {generation_error_details(error)}",
            ) from None
        except (ValueError, TypeError, AttributeError) as error:
            raise LLMServiceError(
                "Не удалось получить корректный ответ языковой модели.",
                code=LLMErrorCode.INVALID_RESPONSE,
                diagnostic_details=f"stage={stage}; {generation_error_details(error)}",
            ) from None

    async def _stream_response(
        self,
        payload: dict[str, Any],
        model: str,
    ) -> LLMResponse:
        async with self._client.chat.completions.stream(
            model=model,
            stream_options={"include_usage": True},
            **payload,
        ) as stream:
            await stream.until_done()

            try:
                completion = stream.current_completion_snapshot
            except AssertionError:
                raise LLMEmptyResponseError("Модель вернула пустой поток ответа.") from None

            return decode_stream_response(completion)
