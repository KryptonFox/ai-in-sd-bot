import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    parse_port,
    parse_timezone,
)


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
    )
