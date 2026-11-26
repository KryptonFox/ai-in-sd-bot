import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values

from app.core.budget import ContextBudget
from app.core.limits import DEFAULT_AGENT_MAX_TOOL_CALLS
from app.core.toml_config import (
    ConfigValueError,
    ContextProvider,
    MCPTransport,
    environment_value,
    load_toml_environment,
    merge_environment,
    parse_boolean,
    parse_choice,
    parse_integer,
    parse_number,
    parse_port,
    parse_timezone,
    parse_url,
    validate_environment_parameter,
)


class ConfigError(ConfigValueError):
    pass


DEFAULT_SCHEDULE_PATH = "mcp_servers/student_tools/data/schedule.json"
LEGACY_DEFAULT_SCHEDULE_PATH = "app/data/schedule.json"
DEFAULT_ROUTER_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEFAULT_ROUTER_MODEL = "gpt-6-luna-decision"
OPENCODE_GO_PATH_PREFIX = "/inference/go/"
MATH_MCP_ENVIRONMENT_KEYS = frozenset(
    {
        "MATH_MCP_CONFIG_FILE",
        "MATH_MCP_STARTUP_TIMEOUT_SECONDS",
        "MATH_MCP_OPERATION_TIMEOUT_SECONDS",
        "MATH_MCP_QUEUE_TIMEOUT_SECONDS",
        "MATH_MCP_MEMORY_LIMIT_MIB",
        "MATH_MCP_MAX_PNG_BYTES",
    }
)
WEB_SEARCH_ENVIRONMENT_KEYS = frozenset(
    {
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_API_KEY",
        "WEB_SEARCH_ENDPOINT",
        "WEB_SEARCH_TIMEOUT_SECONDS",
        "WEB_SEARCH_MAX_RESULTS",
    }
)


def resolve_schedule_path(value: str) -> str:
    if value == LEGACY_DEFAULT_SCHEDULE_PATH:
        return DEFAULT_SCHEDULE_PATH
    return value


def public_url(value: str) -> str:
    if not value:
        return value

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid>"

    if not parsed.scheme or not hostname:
        return "<invalid>"

    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host = f"{host}:{port}"

    return f"{parsed.scheme}://{host}"


def is_opencode_go_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False

    return (
        parsed.scheme == "https"
        and parsed.hostname == "opencode.ai"
        and parsed.path.startswith(OPENCODE_GO_PATH_PREFIX)
    )


def _math_mcp_environment(
    file_values: Mapping[str, str | None],
    process_values: Mapping[str, str],
    config_file: Path | str | None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}

    for source in (file_values, process_values):
        for key, value in source.items():
            if value and key in MATH_MCP_ENVIRONMENT_KEYS:
                overrides[key] = value

    if config_file is not None:
        overrides.setdefault("MATH_MCP_CONFIG_FILE", str(config_file))

    for key, value in overrides.items():
        if key != "MATH_MCP_CONFIG_FILE":
            validate_environment_parameter(key, value)

    return overrides


def _web_search_environment(
    file_values: Mapping[str, str | None],
    process_values: Mapping[str, str],
) -> dict[str, str]:
    overrides: dict[str, str] = {}

    for source in (file_values, process_values):
        for key, value in source.items():
            if value and key in WEB_SEARCH_ENVIRONMENT_KEYS:
                overrides[key] = value

    return overrides


def _bot_token(values: Mapping[str, str | None]) -> str:
    token = environment_value(values, "BOT_TOKEN")

    try:
        validate_token(token)
    except TokenValidationError:
        raise ConfigError("BOT_TOKEN: укажите токен, полученный у BotFather.") from None

    return token


def _postgres_password(values: Mapping[str, str | None]) -> str:
    password = environment_value(values, "POSTGRES_PASSWORD")
    if not password:
        raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")

    return password


def _telegram_proxy(values: Mapping[str, str | None]) -> str:
    proxy = environment_value(values, "TELEGRAM_PROXY_URL")
    if not proxy:
        return proxy

    try:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "socks5"} or not parsed.hostname:
            raise ValueError
    except Exception:
        raise ConfigError(
            "TELEGRAM_PROXY_URL: нужен адрес вида http://user:pass@host:port или socks5://host:port."
        ) from None

    return proxy


class _DatabaseValues(TypedDict):
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str


class _LLMValues(TypedDict):
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout: float
    llm_streaming: bool
    llm_opencode_session: bool
    llm_max_tokens: int
    llm_generation_reserve_tokens: int
    llm_context_provider: ContextProvider


class _RouterValues(TypedDict):
    router_api_key: str
    router_base_url: str
    router_model: str
    router_timeout: float
    router_opencode_session: bool


class _HistoryValues(TypedDict):
    history_max_messages: int
    history_max_chars: int
    default_context_length: int
    compaction_threshold_messages: int
    compaction_keep_recent: int
    summary_max_chars: int


class _AgentValues(TypedDict):
    agent_timeout: float
    tool_result_max_chars: int
    user_queue_max_size: int
    agent_max_tool_calls: int
    agent_max_llm_steps: int
    confirmation_ttl: int


class _MCPValues(TypedDict):
    mcp_transport: MCPTransport
    mcp_server_url: str
    math_mcp_server_url: str
    mcp_shared_secret: str
    mcp_timeout: float
    math_mcp_request_timeout_seconds: float
    math_mcp_environment: Mapping[str, str]


class _ServiceValues(TypedDict):
    log_level: str
    health_port: int
    schedule_path: str
    schedule_timezone: str


def _parse_database_settings(values: Mapping[str, str | None]) -> _DatabaseValues:
    return {
        "postgres_host": environment_value(values, "POSTGRES_HOST", "127.0.0.1"),
        "postgres_port": parse_port(
            "POSTGRES_PORT",
            environment_value(values, "POSTGRES_PORT", "5432"),
        ),
        "postgres_db": environment_value(values, "POSTGRES_DB", "bot"),
        "postgres_user": environment_value(values, "POSTGRES_USER", "bot"),
    }


def _parse_llm_settings(values: Mapping[str, str | None]) -> _LLMValues:
    return {
        "llm_api_key": environment_value(values, "LLM_API_KEY").strip(),
        "llm_base_url": parse_url(
            "LLM_BASE_URL",
            environment_value(
                values,
                "LLM_BASE_URL",
                "https://openrouter.ai/api/v1",
            ).strip(),
        ),
        "llm_model": environment_value(values, "LLM_MODEL", "openai/gpt-4o-mini").strip()
        or "openai/gpt-4o-mini",
        "llm_timeout": parse_number(
            "LLM_TIMEOUT",
            environment_value(values, "LLM_TIMEOUT", "180.0"),
        ),
        "llm_streaming": parse_boolean(
            "LLM_STREAMING",
            environment_value(values, "LLM_STREAMING", "true"),
        ),
        "llm_opencode_session": parse_boolean(
            "LLM_OPENCODE_SESSION",
            environment_value(values, "LLM_OPENCODE_SESSION", "false"),
        ),
        "llm_max_tokens": parse_integer(
            "LLM_MAX_TOKENS",
            environment_value(values, "LLM_MAX_TOKENS", "16384"),
        ),
        "llm_generation_reserve_tokens": parse_integer(
            "LLM_GENERATION_RESERVE_TOKENS",
            environment_value(values, "LLM_GENERATION_RESERVE_TOKENS", "4096"),
        ),
        "llm_context_provider": ContextProvider(
            parse_choice(
                "LLM_CONTEXT_PROVIDER",
                environment_value(
                    values,
                    "LLM_CONTEXT_PROVIDER",
                    ContextProvider.AUTO.value,
                ),
            )
        ),
    }


def _parse_router_settings(values: Mapping[str, str | None]) -> _RouterValues:
    return {
        "router_api_key": environment_value(values, "ROUTER_API_KEY").strip(),
        "router_base_url": parse_url(
            "ROUTER_BASE_URL",
            environment_value(
                values,
                "ROUTER_BASE_URL",
                DEFAULT_ROUTER_BASE_URL,
            ).strip(),
        ),
        "router_model": environment_value(
            values,
            "ROUTER_MODEL",
            DEFAULT_ROUTER_MODEL,
        ).strip()
        or DEFAULT_ROUTER_MODEL,
        "router_timeout": parse_number(
            "ROUTER_TIMEOUT",
            environment_value(values, "ROUTER_TIMEOUT", "30.0"),
        ),
        "router_opencode_session": parse_boolean(
            "ROUTER_OPENCODE_SESSION",
            environment_value(values, "ROUTER_OPENCODE_SESSION", "false"),
        ),
    }


def _parse_history_settings(values: Mapping[str, str | None]) -> _HistoryValues:
    return {
        "history_max_messages": parse_integer(
            "HISTORY_MAX_MESSAGES",
            environment_value(values, "HISTORY_MAX_MESSAGES", "10"),
        ),
        "history_max_chars": parse_integer(
            "HISTORY_MAX_CHARS",
            environment_value(values, "HISTORY_MAX_CHARS", "8000"),
        ),
        "default_context_length": parse_integer(
            "DEFAULT_CONTEXT_LENGTH",
            environment_value(values, "DEFAULT_CONTEXT_LENGTH", "32768"),
        ),
        "compaction_threshold_messages": parse_integer(
            "COMPACTION_THRESHOLD_MESSAGES",
            environment_value(values, "COMPACTION_THRESHOLD_MESSAGES", "10"),
        ),
        "compaction_keep_recent": parse_integer(
            "COMPACTION_KEEP_RECENT",
            environment_value(values, "COMPACTION_KEEP_RECENT", "3"),
        ),
        "summary_max_chars": parse_integer(
            "SUMMARY_MAX_CHARS",
            environment_value(values, "SUMMARY_MAX_CHARS", "2000"),
        ),
    }


def _parse_agent_settings(values: Mapping[str, str | None]) -> _AgentValues:
    return {
        "agent_timeout": parse_number(
            "AGENT_TIMEOUT",
            environment_value(values, "AGENT_TIMEOUT", "240.0"),
        ),
        "agent_max_tool_calls": parse_integer(
            "AGENT_MAX_TOOL_CALLS",
            environment_value(
                values,
                "AGENT_MAX_TOOL_CALLS",
                str(DEFAULT_AGENT_MAX_TOOL_CALLS),
            ),
        ),
        "agent_max_llm_steps": parse_integer(
            "AGENT_MAX_LLM_STEPS",
            environment_value(values, "AGENT_MAX_LLM_STEPS", "4"),
        ),
        "confirmation_ttl": parse_integer(
            "CONFIRMATION_TTL",
            environment_value(values, "CONFIRMATION_TTL", "300"),
        ),
        "tool_result_max_chars": parse_integer(
            "TOOL_RESULT_MAX_CHARS",
            environment_value(values, "TOOL_RESULT_MAX_CHARS", "12000"),
        ),
        "user_queue_max_size": parse_integer(
            "USER_QUEUE_MAX_SIZE",
            environment_value(values, "USER_QUEUE_MAX_SIZE", "3"),
        ),
    }


def _parse_mcp_settings(
    values: Mapping[str, str | None],
    math_environment: Mapping[str, str],
) -> _MCPValues:
    return {
        "mcp_transport": MCPTransport(
            parse_choice(
                "MCP_TRANSPORT",
                environment_value(values, "MCP_TRANSPORT", MCPTransport.STDIO.value),
            )
        ),
        "mcp_server_url": parse_url(
            "MCP_SERVER_URL",
            environment_value(
                values,
                "MCP_SERVER_URL",
                "http://127.0.0.1:8000/mcp/",
            ).strip(),
        ),
        "math_mcp_server_url": parse_url(
            "MATH_MCP_SERVER_URL",
            environment_value(values, "MATH_MCP_SERVER_URL").strip(),
        ),
        "mcp_shared_secret": environment_value(values, "MCP_SHARED_SECRET"),
        "mcp_timeout": parse_number(
            "MCP_TIMEOUT",
            environment_value(values, "MCP_TIMEOUT", "10.0"),
        ),
        "math_mcp_request_timeout_seconds": parse_number(
            "MATH_MCP_REQUEST_TIMEOUT_SECONDS",
            environment_value(values, "MATH_MCP_REQUEST_TIMEOUT_SECONDS", "40.0"),
        ),
        "math_mcp_environment": math_environment,
    }


def _parse_service_settings(values: Mapping[str, str | None]) -> _ServiceValues:
    return {
        "log_level": parse_choice(
            "LOG_LEVEL",
            environment_value(values, "LOG_LEVEL", "INFO").upper(),
        ),
        "health_port": parse_port(
            "HEALTH_PORT",
            environment_value(values, "HEALTH_PORT", "8080"),
        ),
        "schedule_path": resolve_schedule_path(
            environment_value(values, "SCHEDULE_PATH", DEFAULT_SCHEDULE_PATH)
        ),
        "schedule_timezone": parse_timezone(
            "SCHEDULE_TIMEZONE",
            environment_value(values, "SCHEDULE_TIMEZONE", "Europe/Moscow"),
        ),
    }


def _load_settings(
    env_file: Path | str,
    *,
    environ: Mapping[str, str] | None,
    config_file: Path | str | None,
) -> "Settings":
    file_values = dotenv_values(env_file, interpolate=False)
    process_values = os.environ if environ is None else environ
    toml_values = load_toml_environment(config_file, required=False)
    values = merge_environment(toml_values, file_values, process_values)
    math_environment = _math_mcp_environment(file_values, process_values, config_file)
    web_search_environment = _web_search_environment(file_values, process_values)
    llm_values = _parse_llm_settings(values)

    return Settings(
        bot_token=_bot_token(values),
        postgres_password=_postgres_password(values),
        telegram_proxy_url=_telegram_proxy(values),
        **_parse_database_settings(values),
        **llm_values,
        **_parse_router_settings(values),
        **_parse_history_settings(values),
        **_parse_agent_settings(values),
        **_parse_mcp_settings(values, math_environment),
        **_parse_service_settings(values),
        web_search_environment=web_search_environment,
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    postgres_password: str = field(repr=False)
    telegram_proxy_url: str = field(default="", repr=False)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "bot"
    postgres_user: str = "bot"
    log_level: str = "INFO"
    health_port: int = 8080
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "openai/gpt-4o-mini"
    llm_timeout: float = 180.0
    llm_streaming: bool = True
    llm_opencode_session: bool = False
    llm_max_tokens: int = 16384
    llm_generation_reserve_tokens: int = 4096
    router_api_key: str = field(default="", repr=False)
    router_base_url: str = DEFAULT_ROUTER_BASE_URL
    router_model: str = DEFAULT_ROUTER_MODEL
    router_timeout: float = 30.0
    router_opencode_session: bool = False
    agent_timeout: float = 240.0
    summary_max_chars: int = 2000
    tool_result_max_chars: int = 12000
    llm_context_provider: ContextProvider = ContextProvider.AUTO
    history_max_messages: int = 10
    history_max_chars: int = 8000
    default_context_length: int = 32768
    compaction_threshold_messages: int = 10
    compaction_keep_recent: int = 3
    user_queue_max_size: int = 3
    mcp_transport: MCPTransport = MCPTransport.STDIO
    mcp_server_url: str = "http://127.0.0.1:8000/mcp/"
    math_mcp_server_url: str = ""
    mcp_shared_secret: str = field(default="", repr=False)
    mcp_timeout: float = 10.0
    math_mcp_request_timeout_seconds: float = 40.0
    agent_max_tool_calls: int = DEFAULT_AGENT_MAX_TOOL_CALLS
    agent_max_llm_steps: int = 4
    confirmation_ttl: int = 300
    schedule_path: str = DEFAULT_SCHEDULE_PATH
    schedule_timezone: str = "Europe/Moscow"
    math_mcp_environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    web_search_environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def effective_router_api_key(self) -> str:
        if self.router_api_key:
            return self.router_api_key
        if is_opencode_go_url(self.router_base_url) and is_opencode_go_url(self.llm_base_url):
            return self.llm_api_key

        return ""

    @property
    def effective_generation_reserve_tokens(self) -> int:
        fallback = ContextBudget(
            self.default_context_length,
            min(self.llm_max_tokens, self.default_context_length // 2),
            self.llm_generation_reserve_tokens,
        )
        return fallback.effective_generation_reserve

    def diagnostics(self) -> dict[str, object]:
        return {
            "postgres_host": self.postgres_host,
            "postgres_port": self.postgres_port,
            "postgres_db": self.postgres_db,
            "postgres_user": self.postgres_user,
            "log_level": self.log_level,
            "health_port": self.health_port,
            "llm_model": self.llm_model,
            "llm_base_url": public_url(self.llm_base_url),
            "llm_timeout_seconds": self.llm_timeout,
            "llm_max_tokens": self.llm_max_tokens,
            "llm_generation_reserve_tokens": self.llm_generation_reserve_tokens,
            "llm_effective_generation_reserve_tokens": (self.effective_generation_reserve_tokens),
            "llm_context_provider": self.llm_context_provider.value,
            "router_base_url": public_url(self.router_base_url),
            "router_model": self.router_model,
            "router_timeout_seconds": self.router_timeout,
            "router_opencode_session": self.router_opencode_session,
            "agent_timeout_seconds": self.agent_timeout,
            "agent_max_tool_calls": self.agent_max_tool_calls,
            "agent_max_llm_steps": self.agent_max_llm_steps,
            "confirmation_ttl_seconds": self.confirmation_ttl,
            "tool_result_max_chars": self.tool_result_max_chars,
            "history_max_messages": self.history_max_messages,
            "history_max_chars": self.history_max_chars,
            "default_context_length": self.default_context_length,
            "compaction_threshold_messages": self.compaction_threshold_messages,
            "compaction_keep_recent": self.compaction_keep_recent,
            "summary_max_chars": self.summary_max_chars,
            "user_queue_max_size": self.user_queue_max_size,
            "mcp_transport": self.mcp_transport.value,
            "mcp_server_url": public_url(self.mcp_server_url),
            "math_mcp_server_url": public_url(self.math_mcp_server_url),
            "mcp_timeout_seconds": self.mcp_timeout,
            "math_mcp_request_timeout_seconds": self.math_mcp_request_timeout_seconds,
            "schedule_path": self.schedule_path,
            "schedule_timezone": self.schedule_timezone,
        }

    @classmethod
    def load(
        cls,
        env_file: Path | str = ".env",
        *,
        environ: Mapping[str, str] | None = None,
        config_file: Path | str | None = None,
    ) -> "Settings":
        try:
            return _load_settings(env_file, environ=environ, config_file=config_file)
        except ConfigError:
            raise
        except ConfigValueError as error:
            raise ConfigError(str(error)) from None
