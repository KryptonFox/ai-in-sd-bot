import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.limits import POSTGRES_INTEGER_MAX

DEFAULT_CONFIG_FILE = "config.toml"

type TomlKind = Literal["string", "integer", "number", "boolean"]


class ConfigValueError(ValueError):
    pass


class TomlConfigError(ConfigValueError):
    pass


class MCPTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


class ContextProvider(StrEnum):
    AUTO = "auto"
    NONE = "none"
    OLLAMA = "ollama"
    MODELS = "models"


@dataclass(frozen=True)
class TomlParameter:
    environment_name: str
    kind: TomlKind
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    allow_query: bool = False


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
            choices=tuple(provider.value for provider in ContextProvider),
        ),
    },
    "router": {
        "base_url": TomlParameter("ROUTER_BASE_URL", "string"),
        "model": TomlParameter("ROUTER_MODEL", "string"),
        "timeout_seconds": TomlParameter("ROUTER_TIMEOUT", "number", 1, 1800),
        "opencode_session": TomlParameter("ROUTER_OPENCODE_SESSION", "boolean"),
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
            choices=tuple(transport.value for transport in MCPTransport),
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
    "web_search": {
        "provider": TomlParameter(
            "WEB_SEARCH_PROVIDER",
            "string",
            choices=("disabled", "brave", "exa"),
        ),
        "endpoint": TomlParameter("WEB_SEARCH_ENDPOINT", "string", allow_query=True),
        "timeout_seconds": TomlParameter("WEB_SEARCH_TIMEOUT_SECONDS", "number", 1, 30),
        "max_results": TomlParameter("WEB_SEARCH_MAX_RESULTS", "integer", 1, 8),
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

ENVIRONMENT_ONLY_PARAMETERS = (
    TomlParameter("POSTGRES_PORT", "integer", 1, 65535),
    TomlParameter("HEALTH_PORT", "integer", 1, 65535),
    TomlParameter("MCP_PORT", "integer", 1, 65535),
    TomlParameter(
        "LOG_LEVEL",
        "string",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
)

ENVIRONMENT_PARAMETERS: dict[str, TomlParameter] = {
    parameter.environment_name: parameter
    for section in TOML_SCHEMA.values()
    for parameter in section.values()
}
ENVIRONMENT_PARAMETERS.update(
    {parameter.environment_name: parameter for parameter in ENVIRONMENT_ONLY_PARAMETERS}
)

PUBLIC_URL_ENVIRONMENT_NAMES = frozenset(
    {
        "LLM_BASE_URL",
        "ROUTER_BASE_URL",
        "MCP_SERVER_URL",
        "MATH_MCP_SERVER_URL",
        "WEB_SEARCH_ENDPOINT",
    }
)


class UrlValidationError(ValueError):
    pass


def _parameter_error(section: str, key: str, message: str) -> TomlConfigError:
    return TomlConfigError(f"config.toml: [{section}].{key}: {message}")


def _section_error(section: str, message: str) -> TomlConfigError:
    return TomlConfigError(f"config.toml: [{section}]: {message}")


def validate_url(raw_value: str, *, allow_query: bool = False) -> str:
    if not raw_value:
        return raw_value

    try:
        parsed = urlsplit(raw_value)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError:
        raise UrlValidationError("укажите корректный URL (http:// или https://).") from None

    if username is not None or password is not None:
        raise UrlValidationError("не храните учётные данные в URL.")
    if parsed.fragment or parsed.query and not allow_query:
        raise UrlValidationError("не храните параметры запроса и фрагменты в URL.")
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise UrlValidationError("укажите корректный URL (http:// или https://).")

    return raw_value


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
            raise _parameter_error(
                section,
                key,
                f"допустимые значения: {', '.join(parameter.choices)}.",
            )

        if parameter.environment_name in PUBLIC_URL_ENVIRONMENT_NAMES and raw_value:
            try:
                validate_url(raw_value, allow_query=parameter.allow_query)
            except UrlValidationError as error:
                raise _parameter_error(section, key, str(error)) from None

        return raw_value

    if parameter.minimum is not None and number < parameter.minimum:
        raise _parameter_error(
            section,
            key,
            f"значение должно быть не меньше {_format_bound(parameter.minimum)}.",
        )
    if parameter.maximum is not None and number > parameter.maximum:
        raise _parameter_error(
            section,
            key,
            f"значение должно быть не больше {_format_bound(parameter.maximum)}.",
        )

    return number


def _serialized(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


def environment_value(
    values: Mapping[str, str | None],
    name: str,
    default: str = "",
) -> str:
    return values.get(name) or default


def _environment_parameter(name: str) -> TomlParameter:
    parameter = ENVIRONMENT_PARAMETERS.get(name)
    if parameter is None:
        raise ConfigValueError(f"{name}: параметр не описан в схеме конфигурации.")

    return parameter


def _format_bound(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))

    return f"{value:g}"


def _integer_message(name: str, parameter: TomlParameter) -> str:
    if parameter.minimum is not None and parameter.maximum is not None:
        return (
            f"{name}: допустимый диапазон "
            f"{_format_bound(parameter.minimum)}..{_format_bound(parameter.maximum)}."
        )

    if parameter.minimum is not None:
        return f"{name}: значение должно быть целым числом >= {_format_bound(parameter.minimum)}."

    return f"{name}: значение должно быть целым числом."


def _number_message(name: str, parameter: TomlParameter) -> str:
    if parameter.minimum is not None and parameter.maximum is not None:
        return (
            f"{name}: допустимый диапазон "
            f"{_format_bound(parameter.minimum)}..{_format_bound(parameter.maximum)}."
        )

    return f"{name}: значение должно быть конечным числом."


def _choices_message(name: str, choices: tuple[str, ...]) -> str:
    if len(choices) == 1:
        allowed = choices[0]
    else:
        allowed = f"{', '.join(choices[:-1])} или {choices[-1]}"

    return f"{name}: используйте {allowed}."


def parse_integer(name: str, raw_value: str) -> int:
    parameter = _environment_parameter(name)

    try:
        number = int(raw_value)
    except ValueError:
        raise ConfigValueError(_integer_message(name, parameter)) from None

    if parameter.minimum is not None and number < parameter.minimum:
        raise ConfigValueError(_integer_message(name, parameter)) from None
    if parameter.maximum is not None and number > parameter.maximum:
        raise ConfigValueError(_integer_message(name, parameter)) from None

    return number


def parse_number(name: str, raw_value: str) -> float:
    parameter = _environment_parameter(name)

    try:
        number = float(raw_value)
    except ValueError:
        raise ConfigValueError(_number_message(name, parameter)) from None

    if not math.isfinite(number):
        raise ConfigValueError(_number_message(name, parameter)) from None
    if parameter.minimum is not None and number < parameter.minimum:
        raise ConfigValueError(_number_message(name, parameter)) from None
    if parameter.maximum is not None and number > parameter.maximum:
        raise ConfigValueError(_number_message(name, parameter)) from None

    return number


def parse_boolean(name: str, raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized not in {"true", "false", "1", "0"}:
        raise ConfigValueError(f"{name}: укажите true или false.")

    return normalized in {"true", "1"}


def parse_choice(name: str, raw_value: str) -> str:
    parameter = _environment_parameter(name)
    choices = parameter.choices
    if choices is None:
        raise ConfigValueError(f"{name}: параметр не поддерживает выбор значения.")
    if raw_value not in choices:
        raise ConfigValueError(_choices_message(name, choices))

    return raw_value


def parse_url(name: str, raw_value: str) -> str:
    try:
        return validate_url(raw_value)
    except UrlValidationError as error:
        raise ConfigValueError(f"{name}: {error}") from None


def parse_port(name: str, raw_value: str) -> int:
    try:
        number = int(raw_value)
    except ValueError:
        raise ConfigValueError(f"{name}: нужен номер порта от 1 до 65535.") from None

    if not 1 <= number <= 65535:
        raise ConfigValueError(f"{name}: нужен номер порта от 1 до 65535.")

    return number


def parse_timezone(name: str, raw_value: str) -> str:
    try:
        ZoneInfo(raw_value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ConfigValueError(f"{name}: укажите существующую IANA-зону.") from None

    return raw_value


def validate_environment_parameter(name: str, raw_value: str) -> None:
    parameter = _environment_parameter(name)

    if parameter.kind == "integer":
        parse_integer(name, raw_value)
    elif parameter.kind == "number":
        parse_number(name, raw_value)
    elif parameter.kind == "boolean":
        parse_boolean(name, raw_value)
    elif parameter.choices is not None:
        parse_choice(name, raw_value)


def merge_environment(
    toml_values: Mapping[str, str],
    file_values: Mapping[str, str | None],
    process_values: Mapping[str, str],
) -> dict[str, str | None]:
    file_overrides = {key: value for key, value in file_values.items() if value}
    process_overrides = {key: value for key, value in process_values.items() if value}

    return {**toml_values, **file_overrides, **process_overrides}


def load_toml_environment(
    config_file: Path | str | None = None,
    *,
    required: bool | None = None,
) -> dict[str, str]:
    explicit_path = config_file is not None
    if required is None:
        required = explicit_path

    path = Path(DEFAULT_CONFIG_FILE if config_file is None else config_file)
    if not path.exists():
        if required:
            raise TomlConfigError(f"config.toml: файл не найден: {path}.")

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
