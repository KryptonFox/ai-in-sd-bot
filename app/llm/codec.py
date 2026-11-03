import json
import logging
from typing import Any

from openai.types.chat import ChatCompletion

from app.core.context import get_request_id
from app.core.exceptions import (
    LLMEmptyResponseError,
    LLMErrorCode,
    LLMRefusalError,
    LLMServiceError,
)
from app.core.schemas.llm import FinishReason, LLMRequest, LLMResponse, ToolCall
from app.core.schemas.message import MessageRole
from app.llm.diagnostics import generation_error_details, is_repeated_assistant_role

logger = logging.getLogger("app.llm")


def encode_request(request: LLMRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []

    for message in request.messages:
        item: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.tool_calls:
            if message.role != MessageRole.ASSISTANT:
                raise ValueError("tool_calls require assistant")

            encoded_calls = []
            for call in message.tool_calls:
                encoded_calls.append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                allow_nan=False,
                            ),
                        },
                    }
                )
            item["tool_calls"] = encoded_calls

        if message.role == MessageRole.TOOL:
            if not message.tool_call_id:
                raise ValueError("tool result requires tool_call_id")

            item["tool_call_id"] = message.tool_call_id

        messages.append(item)

    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": request.temperature,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens

    if request.tools:
        encoded_tools = []
        for tool in request.tools:
            encoded_tools.append(
                {"type": "function", "function": tool.model_dump(exclude_none=True)}
            )
        payload["tools"] = encoded_tools

    return payload


def decode_stream_response(completion: ChatCompletion) -> LLMResponse:
    try:
        payload = completion.model_dump(warnings=False)
        _normalize_stream_roles(payload)
        validated = ChatCompletion.model_validate(payload)
    except (ValueError, TypeError, AttributeError) as error:
        raise LLMServiceError(
            "Не удалось проверить формат ответа языковой модели.",
            code=LLMErrorCode.INVALID_RESPONSE,
            diagnostic_details=f"stage=response_validation; {generation_error_details(error)}",
        ) from None

    return decode_response(validated)


def decode_response(data: ChatCompletion) -> LLMResponse:
    if not data.choices:
        raise LLMEmptyResponseError("Модель вернула пустой ответ.")

    choice = data.choices[0]
    message = choice.message
    if message.refusal or choice.finish_reason == "content_filter":
        raise LLMRefusalError("Модель отказалась выполнять запрос.")

    if choice.finish_reason not in {
        FinishReason.STOP,
        FinishReason.LENGTH,
        FinishReason.TOOL_CALLS,
    }:
        raise LLMServiceError(
            "API вернул неподдерживаемую причину завершения ответа.",
            code=LLMErrorCode.INVALID_RESPONSE,
            diagnostic_details=(
                "stage=response_validation; field=choices.0.finish_reason; "
                f"problem=unsupported_finish_reason; received={choice.finish_reason}; "
                "expected=stop|length|tool_calls|content_filter"
            ),
        )

    content = message.content or ""
    tool_calls = []

    for position, call in enumerate(message.tool_calls or []):
        if call.type != "function":
            raise ValueError("unsupported_tool_call")

        arguments = decode_tool_arguments(call.function.arguments, position)
        tool_calls.append(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=arguments,
            )
        )

    if choice.finish_reason == FinishReason.TOOL_CALLS and not tool_calls:
        raise ValueError("missing_tool_calls")

    if not content.strip() and not tool_calls:
        if choice.finish_reason == FinishReason.LENGTH:
            raise LLMServiceError(
                "Лимит генерации исчерпан до появления публичного ответа.",
                code=LLMErrorCode.GENERATION_LIMIT,
            )

        raise LLMEmptyResponseError("Модель вернула пустой ответ.")

    usage = data.usage

    return LLMResponse(
        content=content,
        finish_reason=FinishReason(choice.finish_reason),
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
        tool_calls=tool_calls,
    )


def decode_tool_arguments(value: str, position: int) -> dict[str, Any]:
    try:
        arguments = json.loads(value, parse_constant=_reject_json_constant)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")

        return arguments
    except (ValueError, TypeError) as error:
        raise LLMServiceError(
            "Модель вернула некорректные аргументы инструмента.",
            code=LLMErrorCode.INVALID_RESPONSE,
            diagnostic_details=(
                "stage=tool_arguments; "
                "field=choices.0.message.tool_calls."
                f"{position}.function.arguments; {generation_error_details(error)}"
            ),
        ) from None


def _normalize_stream_roles(payload: dict[str, Any]) -> None:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return

    missing = 0
    repeated = 0

    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role is None:
            message["role"] = "assistant"
            missing += 1
        elif isinstance(role, str) and is_repeated_assistant_role(role):
            message["role"] = "assistant"
            repeated += 1

    if missing or repeated:
        logger.info(
            "[%s] llm_stream_role_normalized missing=%d repeated=%d",
            get_request_id() or "local",
            missing,
            repeated,
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")
