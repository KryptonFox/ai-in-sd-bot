import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from app.config import ConfigError


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
    schedule_path: Path = Path("app/data/schedule.json")
    schedule_timezone: str = "Europe/Moscow"
    mcp_transport: str = "stdio"

    @classmethod
    def load(
        cls,
        env_file: Path | str = ".env",
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "ServerSettings":
        values = {
            **dotenv_values(env_file, interpolate=False),
            **(os.environ if environ is None else environ),
        }

        def value(key: str, default: str = "") -> str:
            return values.get(key) or default

        def port(key: str, default: str) -> int:
            try:
                result = int(value(key, default))
                if not 1 <= result <= 65535:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужен номер порта от 1 до 65535.") from None

        password = value("POSTGRES_PASSWORD")
        if not password:
            raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")

        shared_secret = value("MCP_SHARED_SECRET")
        if len(shared_secret) < 32:
            raise ConfigError("MCP_SHARED_SECRET: задайте секрет длиной не менее 32 символов.")

        timezone = value("SCHEDULE_TIMEZONE", "Europe/Moscow")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigError("SCHEDULE_TIMEZONE: укажите существующую IANA-зону.") from None

        transport = value("MCP_TRANSPORT", "stdio")
        if transport not in {"stdio", "streamable-http"}:
            raise ConfigError("MCP_TRANSPORT: используйте stdio или streamable-http.")

        return cls(
            postgres_host=value("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=port("POSTGRES_PORT", "5432"),
            postgres_db=value("POSTGRES_DB", "bot"),
            postgres_user=value("POSTGRES_USER", "bot"),
            postgres_password=password,
            mcp_shared_secret=shared_secret,
            mcp_host=value("MCP_HOST", "0.0.0.0"),
            mcp_port=port("MCP_PORT", "8000"),
            schedule_path=Path(value("SCHEDULE_PATH", "app/data/schedule.json")),
            schedule_timezone=timezone,
            mcp_transport=transport,
        )
