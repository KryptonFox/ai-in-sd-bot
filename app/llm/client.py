import logging
import re
import time

import httpx

from app.config import Settings
from app.core.context import get_request_id
from app.core.exceptions import LLMEmptyResponseError, LLMServiceError, LLMTimeoutError
from app.core.schemas.llm import LLMRequest, LLMResponse, ToolCall
from app.llm.base import BaseLLMClient

logger = logging.getLogger("app.llm")
DEFAULT_CONTEXT_LENGTH = 32768


class OpenAIClient(BaseLLMClient):
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._cached_context_length: int | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=self._settings.llm_timeout)

    async def get_context_length(self, model: str | None = None) -> int:
        if self._cached_context_length is not None:
            return self._cached_context_length

        target_model = model or self._settings.llm_model
        client_to_use = await self._get_client()
        should_close = self._client is None

        detected: int | None = None
        try:
            detected = await self._detect_context_length(client_to_use, target_model)
        except Exception:
            logger.debug("Сбой автоопределения контекста для %s", target_model)
        finally:
            if should_close:
                await client_to_use.aclose()

        fallback = getattr(self._settings, "default_context_length", DEFAULT_CONTEXT_LENGTH)
        self._cached_context_length = detected or fallback
        logger.info(
            "Контекст модели %s: %d токенов%s",
            target_model,
            self._cached_context_length,
            " (авто)" if detected else " (по умолчанию)",
        )
        return self._cached_context_length

    async def _detect_context_length(
        self, client: httpx.AsyncClient, model_name: str
    ) -> int | None:
        base_url = self._settings.llm_base_url.rstrip("/")

        ollama_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        try:
            resp = await client.post(
                f"{ollama_url}/api/show",
                json={"name": model_name},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                model_info = data.get("model_info", {})
                for k, v in model_info.items():
                    if k.endswith(".context_length") and isinstance(v, int) and v > 0:
                        return v
                params = str(data.get("parameters", ""))
                match = re.search(r"num_ctx\s+(\d+)", params)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        try:
            headers = {}
            if self._settings.llm_api_key:
                headers["Authorization"] = f"Bearer {self._settings.llm_api_key}"
            resp = await client.get(
                f"{base_url}/models",
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    if item.get("id") == model_name:
                        ctx = item.get("context_length") or item.get("max_model_len")
                        if isinstance(ctx, int) and ctx > 0:
                            return ctx
        except Exception:
            pass

        return None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not self._settings.llm_api_key:
            raise LLMServiceError("API-ключ модели не настроен. Укажите LLM_API_KEY в файле .env.")

        url = f"{self._settings.llm_base_url.rstrip('/')}/chat/completions"
        model_name = request.model or self._settings.llm_model
        req_id = get_request_id() or "local"

        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        messages_payload = [
            {"role": msg.role.value, "content": msg.content} for msg in request.messages
        ]
        payload = {
            "model": model_name,
            "temperature": request.temperature,
            "messages": messages_payload,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        logger.info(
            "[%s] Отправка запроса к модели %s (temp=%.1f, сообщений=%d)...",
            req_id,
            model_name,
            request.temperature,
            len(messages_payload),
        )

        start_time = time.monotonic()
        client_to_use = await self._get_client()
        should_close = self._client is None

        try:
            response = await client_to_use.post(
                url, json=payload, headers=headers, timeout=self._settings.llm_timeout
            )
            response.raise_for_status()
            data = response.json()

        except httpx.TimeoutException:
            logger.warning(
                "[%s] Истёк таймаут обращения к модели (%.1f с)", req_id, self._settings.llm_timeout
            )
            raise LLMTimeoutError(
                "Превышено время ожидания ответа от языковой модели. "
                "Попробуйте повторить запрос позже."
            ) from None

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.error("[%s] LLM API вернул ошибку HTTP %d", req_id, status)
            raise LLMServiceError(
                f"Сервис языковой модели временно недоступен (код ошибки: {status})."
            ) from None

        except httpx.RequestError:
            logger.exception("[%s] Сетевая ошибка при обращении к LLM API", req_id)
            raise LLMServiceError(
                "Не удалось установить соединение с сервисом языковой модели. Проверьте сеть."
            ) from None

        finally:
            if should_close:
                await client_to_use.aclose()

        duration = time.monotonic() - start_time
        choices = data.get("choices", [])
        if not choices:
            logger.warning("[%s] Модель вернула пустой список choices", req_id)
            raise LLMEmptyResponseError("Модель вернула пустой ответ.")

        first_choice = choices[0]
        message_data = first_choice.get("message", {})
        content = (message_data.get("content") or message_data.get("reasoning") or "").strip()
        finish_reason = first_choice.get("finish_reason", "stop")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

        logger.info(
            "[%s] Ответ получен за %.2f с (токены: prompt=%s, completion=%s, total=%s)",
            req_id,
            duration,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )

        if not content:
            logger.warning("[%s] Модель вернула пустое содержимое ответа", req_id)
            raise LLMEmptyResponseError("Модель вернула пустой ответ.")

        tool_calls: list[ToolCall] = []
        for tc in message_data.get("tool_calls", []):
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", {}),
                )
            )

        return LLMResponse(
            content=content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls=tool_calls,
        )
