import json
import re

from openai import APIError, APIStatusError
from pydantic import ValidationError

from app.core.exceptions import LLMServiceError

SAFE_PROVIDER_CODES = frozenset(
    {
        "context_length_exceeded",
        "model_not_found",
        "invalid_api_key",
        "insufficient_quota",
        "rate_limit_exceeded",
        "unsupported_parameter",
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "server_error",
        "not_found_error",
        "invalid_value",
        "model_not_available",
        "unsupported_value",
        "invalid_parameter",
        "no_available_endpoints",
        "model_not_supported",
    }
)
SAFE_PROVIDER_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "rate_limit_error",
        "server_error",
        "not_found_error",
        "insufficient_quota",
    }
)
SAFE_PROVIDER_PARAMS = frozenset(
    {
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "tools",
        "tool_choice",
        "response_format",
        "top_p",
        "stream",
        "n",
        "stop",
        "frequency_penalty",
        "presence_penalty",
    }
)
SAFE_VALUE_ERRORS = frozenset(
    {
        "invalid_json_constant",
        "tool_calls require assistant",
        "tool result requires tool_call_id",
        "unsupported_finish_reason",
        "unsupported_tool_call",
        "tool arguments must be an object",
        "missing_tool_calls",
    }
)
RESPONSE_PROBLEM_REASONS = {
    "unsupported_finish_reason": "API вернул неподдерживаемую причину завершения ответа",
    "unsupported_tool_call": "API вернул неподдерживаемый тип вызова инструмента",
    "tool arguments must be an object": "Аргументы инструмента должны быть JSON-объектом",
    "missing_tool_calls": "Ответ завершён как вызов инструментов, но сами вызовы отсутствуют",
    "invalid_json_constant": "JSON содержит недопустимую константу NaN или Infinity",
    "tool_calls require assistant": "В исходном запросе tool_calls указаны у неверной роли",
    "tool result requires tool_call_id": "В исходном запросе результат инструмента не имеет ID",
}
SAFE_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "object",
        "created",
        "model",
        "choices",
        "index",
        "finish_reason",
        "message",
        "role",
        "content",
        "refusal",
        "tool_calls",
        "type",
        "function",
        "name",
        "arguments",
        "usage",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
)
RATE_LIMIT_METRICS = {
    "tokens per minute": "TPM",
    "tokens per day": "TPD",
    "requests per minute": "RPM",
    "requests per day": "RPD",
}
RATE_LIMIT_COUNT_HEADERS = {
    "x-ratelimit-limit-tokens": "token_limit",
    "x-ratelimit-remaining-tokens": "tokens_remaining",
    "x-ratelimit-limit-requests": "request_limit",
    "x-ratelimit-remaining-requests": "requests_remaining",
}
RATE_LIMIT_RESET_HEADERS = {
    "x-ratelimit-reset-tokens": "tokens_reset_after",
    "x-ratelimit-reset-requests": "requests_reset_after",
}
SAFE_RATE_LIMIT_NUMBER = r"[0-9]{1,12}(?:\.[0-9]{1,6})?"
SAFE_RATE_LIMIT_DURATION = rf"(?:{SAFE_RATE_LIMIT_NUMBER}(?:ms|s|m|h|d)){{1,5}}"


def api_status_error_details(error: APIStatusError) -> str:
    return api_error_details(error)


def api_error_details(error: APIError) -> str:
    details: list[str] = []
    status_code = error.status_code if isinstance(error, APIStatusError) else 0
    if type(error.code) is int and 400 <= error.code <= 599:
        details.append(f"provider_status={error.code}")
        status_code = status_code or error.code

    code = _safe_provider_value(error.code, SAFE_PROVIDER_CODES)
    error_type = _safe_provider_value(error.type, SAFE_PROVIDER_TYPES)
    param = _safe_provider_value(error.param, SAFE_PROVIDER_PARAMS)
    body = error.body
    nested = _field(body, "error")
    api_error = nested if nested is not None else body
    body_message = _field(api_error, "message")
    provider_message = body_message if isinstance(body_message, str) else None
    if provider_message is None and isinstance(error.message, str):
        sdk_wrapper = f"Error code: {status_code} -"
        if not error.message.startswith(sdk_wrapper):
            provider_message = error.message

    code = code or _safe_provider_value(_field(api_error, "code"), SAFE_PROVIDER_CODES)
    error_type = error_type or _safe_provider_value(_field(api_error, "type"), SAFE_PROVIDER_TYPES)
    param = param or _safe_provider_value(_field(api_error, "param"), SAFE_PROVIDER_PARAMS)
    if code:
        details.append(f"provider_code={code}")

    if error_type:
        details.append(f"provider_type={error_type}")

    if param:
        details.append(f"provider_param={param}")

    reason_code, reason = _provider_diagnosis(
        provider_message,
        code,
        error_type,
        param,
        status_code,
    )
    details.extend((f"provider_reason_code={reason_code}", f"provider_reason={reason}"))

    if status_code == 429 or code == "rate_limit_exceeded" or error_type == "rate_limit_error":
        details.extend(_rate_limit_details(error, provider_message))

    return "; ".join(details)


def _rate_limit_details(error: APIError, message: str | None) -> list[str]:
    details: list[str] = []
    normalized_message = message.casefold() if message is not None else ""
    for phrase, metric in RATE_LIMIT_METRICS.items():
        if phrase in normalized_message:
            details.append(f"rate_limit_metric={metric}")
            break

    if not isinstance(error, APIStatusError):
        return details

    headers = error.response.headers
    for header, label in RATE_LIMIT_COUNT_HEADERS.items():
        value = headers.get(header)
        if value is not None and re.fullmatch(SAFE_RATE_LIMIT_NUMBER, value):
            details.append(f"{label}={value}")

    for header, label in RATE_LIMIT_RESET_HEADERS.items():
        value = headers.get(header)
        if value is not None and re.fullmatch(SAFE_RATE_LIMIT_DURATION, value):
            details.append(f"{label}={value}")

    retry_after = headers.get("retry-after")
    if retry_after is not None and re.fullmatch(SAFE_RATE_LIMIT_NUMBER, retry_after):
        details.append(f"retry_after_seconds={retry_after}")

    return details


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)

    return None


def _safe_provider_value(value: object, allowed: frozenset[str]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value

    return None


def _provider_diagnosis(
    message: object,
    code: str | None,
    error_type: str | None,
    param: str | None,
    status_code: int,
) -> tuple[str, str]:
    diagnosis = _diagnosis_from_fields(code, error_type, param)
    if diagnosis is not None:
        return diagnosis

    diagnosis = _diagnosis_from_message(message)
    if diagnosis is not None:
        return diagnosis

    status_reasons = {
        401: ("authentication_rejected", "API отклонил учётные данные"),
        403: ("permission_denied", "API запретил доступ к ресурсу"),
        404: ("resource_not_found", "API не нашёл адрес или модель"),
        429: ("rate_limit_or_quota", "API ограничил запросы или квоту"),
    }
    if status_code in status_reasons:
        return status_reasons[status_code]

    if status_code >= 500:
        return "provider_server_error", "API вернул внутреннюю ошибку сервера"

    return "unclassified_api_error", "API вернул ошибку без распознанного описания"


def _diagnosis_from_fields(
    code: str | None, error_type: str | None, param: str | None
) -> tuple[str, str] | None:
    if code == "context_length_exceeded":
        return "context_length_exceeded", "API сообщил, что превышен лимит контекста модели"

    if code in {"model_not_found", "model_not_available", "model_not_supported"}:
        return "model_unavailable", "API сообщил, что модель недоступна или не найдена"

    if code == "no_available_endpoints":
        return (
            "no_available_endpoints",
            "API сообщил, что для модели нет доступных точек подключения",
        )

    if code == "insufficient_quota" or error_type == "insufficient_quota":
        return "insufficient_quota", "API сообщил об исчерпанной квоте"

    if code == "rate_limit_exceeded" or error_type == "rate_limit_error":
        return "rate_limit_exceeded", "API ограничил запросы или число токенов"

    if code in {"unsupported_parameter", "unsupported_value"}:
        suffix = f"_for_{param}" if param else ""
        if param:
            return (
                f"unsupported_parameter{suffix}",
                f"API отклонил {param}: параметр или значение не поддерживается моделью",
            )

        return "unsupported_parameter", "API сообщил о неподдерживаемом параметре или значении"

    if code == "invalid_parameter":
        suffix = f"_for_{param}" if param else ""
        if param:
            return (
                f"invalid_parameter{suffix}",
                f"API отклонил недопустимое значение параметра {param}",
            )

        return "invalid_parameter", "API сообщил о недопустимом значении параметра"

    if error_type == "authentication_error" or code == "invalid_api_key":
        return "authentication_rejected", "API отклонил учётные данные"

    if error_type == "permission_error":
        return "permission_denied", "API запретил доступ к ресурсу"

    if code == "server_error" or error_type == "server_error":
        return "provider_server_error", "API вернул внутреннюю ошибку сервера"

    return None


def _diagnosis_from_message(message: object) -> tuple[str, str] | None:
    text = message.casefold() if isinstance(message, str) else ""
    if "context length" in text:
        return "context_length_exceeded", "API сообщил, что превышен лимит контекста модели"

    if "no endpoints" in text:
        return (
            "no_available_endpoints",
            "API сообщил, что для модели нет доступных точек подключения",
        )

    if any(phrase in text for phrase in ("unsupported", "not supported", "does not support")):
        message_param = _message_param(message)
        if message_param:
            return (
                f"unsupported_parameter_for_{message_param}",
                f"API отклонил {message_param}: параметр или значение не поддерживается моделью",
            )

        return "unsupported_parameter", "API сообщил о неподдерживаемом параметре или значении"

    if "invalid value" in text:
        message_param = _message_param(message)
        if message_param:
            return (
                f"invalid_parameter_for_{message_param}",
                f"API отклонил недопустимое значение параметра {message_param}",
            )

        return "invalid_parameter", "API сообщил о недопустимом значении параметра"

    if "quota" in text:
        return "insufficient_quota", "API сообщил об исчерпанной квоте"

    if "rate limit" in text:
        return "rate_limit_exceeded", "API ограничил запросы или число токенов"

    return None


def _message_param(message: object) -> str | None:
    if not isinstance(message, str):
        return None

    lowered = message.casefold()
    priority = (
        "temperature",
        "tools",
        "tool_choice",
        "max_tokens",
        "response_format",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "stream",
        "stop",
        "n",
        "messages",
        "model",
    )

    for param in priority:
        if re.search(rf"\b{re.escape(param)}\b", lowered):
            return param

    return None


def generation_error_details(error: BaseException) -> str:
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error

    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        details.append(f"exception={type(current).__name__}")
        origin = _error_origin(current)
        if origin:
            details.append(f"origin={origin}")

        if isinstance(current, LLMServiceError) and current.diagnostic_details is not None:
            details.append(current.diagnostic_details)
            break

        if isinstance(current, APIError):
            details.append(api_error_details(current))

        if isinstance(current, json.JSONDecodeError):
            details.append("problem=invalid_json reason=Получен некорректный JSON")
            details.append(f"invalid_json_line={current.lineno} column={current.colno}")
        elif isinstance(current, ValidationError):
            details.append(f"validation_errors={current.error_count()}")

            for failure in current.errors(include_context=False)[:8]:
                path = ".".join(
                    str(part) if isinstance(part, int) or part in SAFE_RESPONSE_FIELDS else "field"
                    for part in failure["loc"]
                )
                details.append(f"field={path} problem={failure['type']}")
                if path.endswith(".message.role"):
                    details.append(f"received_role={_safe_role_value(failure.get('input'))}")
                    details.append("expected_role=assistant")

                reason = {
                    "missing": "Отсутствует обязательное поле ответа",
                    "string_type": "Поле ответа должно быть строкой",
                    "list_type": "Поле ответа должно быть списком",
                    "dict_type": "Поле ответа должно быть объектом",
                    "literal_error": "Поле ответа содержит неподдерживаемое значение",
                }.get(failure["type"])
                if reason:
                    details.append(f"reason={reason}")
        elif type(current) is ValueError and str(current) in SAFE_VALUE_ERRORS:
            details.append(f"problem={current}")
            details.append(f"reason={RESPONSE_PROBLEM_REASONS[str(current)]}")
        elif isinstance(current, AttributeError):
            name = current.name if current.name in SAFE_RESPONSE_FIELDS else "field"
            details.append(f"missing_attribute={name}")
        elif isinstance(current, OSError) and current.errno is not None:
            details.append(f"errno={current.errno}")

        current = current.__cause__ or current.__context__

    return "; ".join(details)


def is_repeated_assistant_role(value: str) -> bool:
    return re.fullmatch(r"(?:assistant){2,}", value) is not None


def _safe_role_value(value: object) -> str:
    if value is None:
        return "missing_or_null"

    if isinstance(value, str):
        if value in {"assistant", "user", "system", "tool", "function", "developer"}:
            return value

        if is_repeated_assistant_role(value):
            return "repeated_assistant"

        return "unrecognized_string"

    return "invalid_type"


def _error_origin(error: BaseException) -> str | None:
    frame = error.__traceback__
    if frame is None:
        return None

    while frame.tb_next is not None:
        frame = frame.tb_next

    module = frame.tb_frame.f_globals.get("__name__", "")
    if not isinstance(module, str) or not module.startswith(
        ("app.llm.", "openai.", "json.", "pydantic.", "pydantic_core.")
    ):
        return None

    return f"{module}.{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}"
