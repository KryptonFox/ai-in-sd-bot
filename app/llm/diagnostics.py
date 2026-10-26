import json

from openai import APIError
from pydantic import ValidationError

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


def generation_error_details(error: BaseException) -> str:
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error

    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        details.append(f"exception={type(current).__name__}")

        if (
            isinstance(current, APIError)
            and isinstance(current.code, str)
            and current.code in SAFE_PROVIDER_CODES
        ):
            details.append(f"provider_code={current.code}")
        if isinstance(current, json.JSONDecodeError):
            details.append(f"invalid_json_line={current.lineno} column={current.colno}")
        elif isinstance(current, ValidationError):
            details.append(f"validation_errors={current.error_count()}")
            for failure in current.errors(include_input=False, include_context=False)[:8]:
                path = ".".join(
                    str(part) if isinstance(part, int) or part in SAFE_RESPONSE_FIELDS else "field"
                    for part in failure["loc"]
                )
                details.append(f"field={path} problem={failure['type']}")
        elif type(current) is ValueError and str(current) in SAFE_VALUE_ERRORS:
            details.append(f"problem={current}")
        elif isinstance(current, OSError) and current.errno is not None:
            details.append(f"errno={current.errno}")

        current = current.__cause__ or current.__context__

    return "; ".join(details)
