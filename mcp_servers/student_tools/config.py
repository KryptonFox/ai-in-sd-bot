import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from dotenv import dotenv_values

from app.config import (
    DEFAULT_SCHEDULE_PATH,
    ConfigError,
    MCPTransport,
    resolve_schedule_path,
)
from app.core.toml_config import (
    ConfigValueError,
    environment_value,
    load_toml_environment,
    merge_environment,
    parse_choice,
    parse_integer,
    parse_number,
    parse_port,
    parse_timezone,
    parse_url,
)

DEFAULT_WEB_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS = 8.0
DEFAULT_WEB_SEARCH_MAX_RESULTS = 5
DEFAULT_EXA_WEB_SEARCH_ENDPOINT = "https://mcp.exa.ai/mcp?tools=web_search_exa"
DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS = 20.0


class WebSearchProviderName(StrEnum):
    DISABLED = "disabled"
    BRAVE = "brave"
    EXA = "exa"


@dataclass(frozen=True)
class ServerSettings:
    postgres_password: str = field(repr=False)
    mcp_shared_secret: str = field(repr=False)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "bot"
    postgres_user: str = "bot"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    schedule_path: Path = Path(DEFAULT_SCHEDULE_PATH)
    schedule_timezone: str = "Europe/Moscow"
    mcp_transport: MCPTransport = MCPTransport.STDIO
    web_search_provider: WebSearchProviderName = WebSearchProviderName.DISABLED
    web_search_api_key: str = field(default="", repr=False)
    web_search_endpoint: str = DEFAULT_WEB_SEARCH_ENDPOINT
    web_search_timeout_seconds: float = DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS
    web_search_max_results: int = DEFAULT_WEB_SEARCH_MAX_RESULTS

    @classmethod
    def load(
        cls,
        env_file: Path | str = ".env",
        *,
        environ: Mapping[str, str] | None = None,
        config_file: Path | str | None = None,
    ) -> "ServerSettings":
        try:
            return _load_server_settings(env_file, environ=environ, config_file=config_file)
        except ConfigError:
            raise
        except ConfigValueError as error:
            raise ConfigError(str(error)) from None


def _load_server_settings(
    env_file: Path | str,
    *,
    environ: Mapping[str, str] | None,
    config_file: Path | str | None,
) -> ServerSettings:
    file_values = dotenv_values(env_file, interpolate=False)
    process_values = os.environ if environ is None else environ
    toml_values = load_toml_environment(config_file, required=False)
    values = merge_environment(toml_values, file_values, process_values)

    password = environment_value(values, "POSTGRES_PASSWORD")
    if not password:
        raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")

    shared_secret = environment_value(values, "MCP_SHARED_SECRET")
    if len(shared_secret) < 32:
        raise ConfigError("MCP_SHARED_SECRET: задайте секрет длиной не менее 32 символов.")

    timezone = parse_timezone(
        "SCHEDULE_TIMEZONE",
        environment_value(values, "SCHEDULE_TIMEZONE", "Europe/Moscow"),
    )
    transport = MCPTransport(
        parse_choice(
            "MCP_TRANSPORT",
            environment_value(values, "MCP_TRANSPORT", MCPTransport.STDIO.value),
        )
    )
    web_provider = WebSearchProviderName(
        parse_choice(
            "WEB_SEARCH_PROVIDER",
            environment_value(
                values,
                "WEB_SEARCH_PROVIDER",
                WebSearchProviderName.DISABLED.value,
            ),
        )
    )
    web_api_key = environment_value(values, "WEB_SEARCH_API_KEY").strip()
    raw_endpoint = environment_value(values, "WEB_SEARCH_ENDPOINT").strip()

    if web_provider is WebSearchProviderName.EXA:
        if not raw_endpoint or raw_endpoint == DEFAULT_WEB_SEARCH_ENDPOINT:
            raw_endpoint = DEFAULT_EXA_WEB_SEARCH_ENDPOINT

        try:
            web_endpoint = normalize_exa_web_search_endpoint(raw_endpoint)
        except ValueError as error:
            raise ConfigError(f"WEB_SEARCH_ENDPOINT: {error}") from None

        default_timeout = DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS
    else:
        web_endpoint = parse_url(
            "WEB_SEARCH_ENDPOINT",
            raw_endpoint or DEFAULT_WEB_SEARCH_ENDPOINT,
        )
        default_timeout = DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS

    web_timeout = parse_number(
        "WEB_SEARCH_TIMEOUT_SECONDS",
        environment_value(
            values,
            "WEB_SEARCH_TIMEOUT_SECONDS",
            str(default_timeout),
        ),
    )
    web_max_results = parse_integer(
        "WEB_SEARCH_MAX_RESULTS",
        environment_value(
            values,
            "WEB_SEARCH_MAX_RESULTS",
            str(DEFAULT_WEB_SEARCH_MAX_RESULTS),
        ),
    )

    if web_provider is WebSearchProviderName.BRAVE:
        if not web_api_key:
            raise ConfigError("WEB_SEARCH_API_KEY: задайте ключ для выбранного поисковика.")
        _validate_web_search_endpoint(web_endpoint)

    return ServerSettings(
        postgres_host=environment_value(values, "POSTGRES_HOST", "127.0.0.1"),
        postgres_port=parse_port(
            "POSTGRES_PORT",
            environment_value(values, "POSTGRES_PORT", "5432"),
        ),
        postgres_db=environment_value(values, "POSTGRES_DB", "bot"),
        postgres_user=environment_value(values, "POSTGRES_USER", "bot"),
        postgres_password=password,
        mcp_shared_secret=shared_secret,
        mcp_host=environment_value(values, "MCP_HOST", "0.0.0.0"),
        mcp_port=parse_port(
            "MCP_PORT",
            environment_value(values, "MCP_PORT", "8000"),
        ),
        schedule_path=Path(
            resolve_schedule_path(environment_value(values, "SCHEDULE_PATH", DEFAULT_SCHEDULE_PATH))
        ),
        schedule_timezone=timezone,
        mcp_transport=transport,
        web_search_provider=web_provider,
        web_search_api_key=web_api_key,
        web_search_endpoint=web_endpoint,
        web_search_timeout_seconds=web_timeout,
        web_search_max_results=web_max_results,
    )


def _validate_web_search_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("WEB_SEARCH_ENDPOINT: укажите HTTPS-адрес поискового API.") from None

    if (
        parsed.scheme != "https"
        or not hostname
        or port is not None
        and not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("WEB_SEARCH_ENDPOINT: укажите HTTPS-адрес поискового API.")


def normalize_exa_web_search_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("укажите HTTPS URL hosted Exa MCP.") from None

    query = parse_qs(parsed.query, keep_blank_values=True)
    tools = query.get("tools")

    if (
        parsed.scheme != "https"
        or not hostname
        or port is not None
        and not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or set(query) - {"tools"}
        or tools is not None
        and tools != ["web_search_exa"]
    ):
        raise ValueError("укажите HTTPS URL hosted Exa MCP с tools=web_search_exa.")

    if tools is None:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "tools=web_search_exa", ""))

    return value
