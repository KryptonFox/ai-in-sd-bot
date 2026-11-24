import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from app.core.limits import POSTGRES_INTEGER_MAX

DEFAULT_CONFIG_FILE = "config.toml"

type TomlKind = Literal["string", "integer", "number", "boolean"]


class TomlConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TomlParameter:
    environment_name: str
    kind: TomlKind
    minimum: float | None = None
    maximum: float | None = None
    choices: frozenset[str] | None = None


TOML_SCHEMA: dict[str, dict[str, TomlParameter]] = {
    "llm": {
        "model": TomlParameter("LLM_MODEL", "string"),
        "base_url": TomlParameter("LLM_BASE_URL", "string"),
        "timeout_seconds": TomlParameter("LLM_TIMEOUT", "number", 1, 1800),
        "max_tokens": TomlParameter("LLM_MAX_TOKENS", "integer", 64, 131072),
        "generation_reserve_tokens": TomlParameter(
            "LLM_GENERATION_RESERVE_TOKENS",
            "integer",
            64,
            131072,
        ),
        "streaming": TomlParameter("LLM_STREAMING", "boolean"),
        "opencode_session": TomlParameter("LLM_OPENCODE_SESSION", "boolean"),
        "context_provider": TomlParameter(
            "LLM_CONTEXT_PROVIDER",
            "string",
            choices=frozenset({"auto", "none", "ollama", "models"}),
        ),
    },
    "agent": {
        "timeout_seconds": TomlParameter("AGENT_TIMEOUT", "number", 1, 1800),
        "max_tool_calls": TomlParameter(
            "AGENT_MAX_TOOL_CALLS",
            "integer",
            1,
            POSTGRES_INTEGER_MAX,
        ),
        "max_llm_steps": TomlParameter("AGENT_MAX_LLM_STEPS", "integer", 1, 8),
        "confirmation_ttl_seconds": TomlParameter("CONFIRMATION_TTL", "integer", 1, 300),
        "tool_result_max_chars": TomlParameter(
            "TOOL_RESULT_MAX_CHARS",
            "integer",
            100,
            64000,
        ),
        "user_queue_max_size": TomlParameter("USER_QUEUE_MAX_SIZE", "integer", 1, 10000),
    },
    "history": {
        "max_messages": TomlParameter("HISTORY_MAX_MESSAGES", "integer", 1, 10000),
        "max_chars": TomlParameter("HISTORY_MAX_CHARS", "integer", 100, 1_000_000),
        "default_context_length": TomlParameter(
            "DEFAULT_CONTEXT_LENGTH",
            "integer",
            512,
            2_097_152,
        ),
        "compaction_threshold_messages": TomlParameter(
            "COMPACTION_THRESHOLD_MESSAGES",
            "integer",
            2,
            10000,
        ),
        "compaction_keep_recent": TomlParameter(
            "COMPACTION_KEEP_RECENT",
            "integer",
            1,
            1000,
        ),
        "summary_max_chars": TomlParameter("SUMMARY_MAX_CHARS", "integer", 100, 8000),
    },
    "mcp": {
        "transport": TomlParameter(
            "MCP_TRANSPORT",
            "string",
            choices=frozenset({"stdio", "streamable-http"}),
        ),
        "server_url": TomlParameter("MCP_SERVER_URL", "string"),
        "math_server_url": TomlParameter("MATH_MCP_SERVER_URL", "string"),
        "timeout_seconds": TomlParameter("MCP_TIMEOUT", "number", 1, 120),
        "math_request_timeout_seconds": TomlParameter(
            "MATH_MCP_REQUEST_TIMEOUT_SECONDS",
            "number",
            1,
            180,
        ),
    },
    "math_mcp": {
        "startup_timeout_seconds": TomlParameter(
            "MATH_MCP_STARTUP_TIMEOUT_SECONDS",
            "number",
            1,
            120,
        ),
        "operation_timeout_seconds": TomlParameter(
            "MATH_MCP_OPERATION_TIMEOUT_SECONDS",
            "number",
            0.1,
            60,
        ),
        "queue_timeout_seconds": TomlParameter(
            "MATH_MCP_QUEUE_TIMEOUT_SECONDS",
            "number",
            0.1,
            180,
        ),
        "memory_limit_mib": TomlParameter("MATH_MCP_MEMORY_LIMIT_MIB", "integer", 128, 4096),
        "max_png_bytes": TomlParameter(
            "MATH_MCP_MAX_PNG_BYTES",
            "integer",
            1024,
            256 * 1024,
        ),
    },
}

PUBLIC_URL_ENVIRONMENT_NAMES = frozenset(
    {
        "LLM_BASE_URL",
        "MCP_SERVER_URL",
        "MATH_MCP_SERVER_URL",
    }
)


def _parameter_error(section: str, key: str, message: str) -> TomlConfigError:
    return TomlConfigError(f"config.toml: [{section}].{key}: {message}")


def _section_error(section: str, message: str) -> TomlConfigError:
    return TomlConfigError(f"config.toml: [{section}]: {message}")


def _validated_value(
    section: str,
    key: str,
    raw_value: object,
    parameter: TomlParameter,
) -> str | int | float | bool:
    if parameter.kind == "boolean":
        if not isinstance(raw_value, bool):
            raise _parameter_error(section, key, "укажите true или false.")

        return raw_value

    if parameter.kind == "integer":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise _parameter_error(section, key, "значение должно быть целым числом.")

        number: int | float = raw_value
    elif parameter.kind == "number":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise _parameter_error(section, key, "значение должно быть числом.")

        number = float(raw_value)
        if not math.isfinite(number):
            raise _parameter_error(section, key, "значение должно быть конечным числом.")
    else:
        if not isinstance(raw_value, str):
            raise _parameter_error(section, key, "значение должно быть строкой.")

        if parameter.choices is not None and raw_value not in parameter.choices:
            allowed = ", ".join(sorted(parameter.choices))
            raise _parameter_error(section, key, f"допустимые значения: {allowed}.")

        if parameter.environment_name in PUBLIC_URL_ENVIRONMENT_NAMES and raw_value:
            try:
                parsed = urlsplit(raw_value)
                username = parsed.username
                password = parsed.password
                hostname = parsed.hostname
            except ValueError:
                raise _parameter_error(
                    section,
                    key,
                    "укажите корректный URL (http:// или https://).",
                ) from None

            if username is not None or password is not None:
                raise _parameter_error(
                    section,
                    key,
                    "не храните учётные данные в config.toml; используйте .env или окружение.",
                )
            if parsed.query or parsed.fragment:
                raise _parameter_error(
                    section,
                    key,
                    "не храните параметры запроса и фрагменты в config.toml.",
                )
            if parsed.scheme not in {"http", "https"} or not hostname:
                raise _parameter_error(
                    section, key, "укажите корректный URL (http:// или https://)."
                )

        return raw_value

    if parameter.minimum is not None and number < parameter.minimum:
        raise _parameter_error(
            section,
            key,
            f"значение должно быть не меньше {parameter.minimum:g}.",
        )
    if parameter.maximum is not None and number > parameter.maximum:
        raise _parameter_error(
            section,
            key,
            f"значение должно быть не больше {parameter.maximum:g}.",
        )

    return number


def _serialized(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


def merge_environment(
    toml_values: Mapping[str, str],
    file_values: Mapping[str, str | None],
    process_values: Mapping[str, str],
) -> dict[str, str | None]:
    file_overrides = {key: value for key, value in file_values.items() if value}
    process_overrides = {key: value for key, value in process_values.items() if value}

    return {**toml_values, **file_overrides, **process_overrides}


def load_toml_environment(config_file: Path | str = DEFAULT_CONFIG_FILE) -> dict[str, str]:
    path = Path(config_file)
    if not path.exists():
        return {}

    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise TomlConfigError("config.toml: не удалось прочитать или разобрать файл.") from None

    environment: dict[str, str] = {}

    for section_name, raw_section in document.items():
        schema = TOML_SCHEMA.get(section_name)
        if schema is None:
            raise _section_error(section_name, "неизвестная секция.")

        if not isinstance(raw_section, dict):
            raise _section_error(section_name, "секция должна быть таблицей параметров.")

        for key, raw_value in raw_section.items():
            parameter = schema.get(key)
            if parameter is None:
                raise _parameter_error(section_name, key, "неизвестный параметр.")

            value = _validated_value(section_name, key, raw_value, parameter)
            environment[parameter.environment_name] = _serialized(value)

    return environment
