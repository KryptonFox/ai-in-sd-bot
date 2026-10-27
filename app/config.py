import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


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
    llm_max_tokens: int = 1024
    agent_timeout: float = 240.0
    summary_max_chars: int = 2000
    tool_result_max_chars: int = 12000
    llm_context_provider: str = "auto"
    history_max_messages: int = 10
    history_max_chars: int = 8000
    default_context_length: int = 32768
    compaction_threshold_messages: int = 10
    compaction_keep_recent: int = 3
    user_queue_max_size: int = 3
    mcp_transport: str = "stdio"
    mcp_server_url: str = "http://127.0.0.1:8000/mcp/"
    mcp_shared_secret: str = field(default="", repr=False)
    mcp_timeout: float = 10.0
    agent_max_tool_calls: int = 2
    agent_max_llm_steps: int = 4
    confirmation_ttl: int = 300
    schedule_path: str = "app/data/schedule.json"
    schedule_timezone: str = "Europe/Moscow"

    @classmethod
    def load(
        cls, env_file: Path | str = ".env", *, environ: Mapping[str, str] | None = None
    ) -> "Settings":
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

        token = value("BOT_TOKEN")
        try:
            validate_token(token)
        except TokenValidationError:
            raise ConfigError("BOT_TOKEN: укажите токен, полученный у BotFather.") from None

        password = value("POSTGRES_PASSWORD")
        if not password:
            raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")

        proxy = value("TELEGRAM_PROXY_URL")
        if proxy:
            try:
                parsed = urlsplit(proxy)
                if parsed.scheme not in {"http", "socks5"} or not parsed.hostname:
                    raise ValueError
            except Exception:
                raise ConfigError(
                    "TELEGRAM_PROXY_URL: нужен адрес вида http://user:pass@host:port или socks5://host:port."
                ) from None

        level = value("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL: используйте DEBUG, INFO, WARNING, ERROR или CRITICAL.")

        llm_key = value("LLM_API_KEY", "").strip()
        llm_url = value("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip()
        if llm_url:
            parsed_url = urlsplit(llm_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ConfigError("LLM_BASE_URL: укажите корректный URL (http:// или https://).")

        llm_model = value("LLM_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"

        try:
            llm_timeout = float(value("LLM_TIMEOUT", "180.0"))
            if not math.isfinite(llm_timeout) or llm_timeout <= 0:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "LLM_TIMEOUT: значение должно быть положительным числом секунд."
            ) from None

        try:
            max_messages = int(value("HISTORY_MAX_MESSAGES", "10"))
            if max_messages < 1:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "HISTORY_MAX_MESSAGES: значение должно быть целым числом >= 1."
            ) from None

        try:
            max_chars = int(value("HISTORY_MAX_CHARS", "8000"))
            if max_chars < 100:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "HISTORY_MAX_CHARS: значение должно быть целым числом >= 100."
            ) from None

        try:
            def_ctx = int(value("DEFAULT_CONTEXT_LENGTH", "32768"))
            if def_ctx < 512:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "DEFAULT_CONTEXT_LENGTH: значение должно быть целым числом >= 512."
            ) from None

        try:
            compact_thresh = int(value("COMPACTION_THRESHOLD_MESSAGES", "10"))
            if compact_thresh < 2:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "COMPACTION_THRESHOLD_MESSAGES: значение должно быть целым числом >= 2."
            ) from None

        try:
            compact_keep = int(value("COMPACTION_KEEP_RECENT", "3"))
            if compact_keep < 1:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "COMPACTION_KEEP_RECENT: значение должно быть целым числом >= 1."
            ) from None

        try:
            queue_max = int(value("USER_QUEUE_MAX_SIZE", "3"))
            if queue_max < 1:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "USER_QUEUE_MAX_SIZE: значение должно быть целым числом >= 1."
            ) from None

        context_provider = value("LLM_CONTEXT_PROVIDER", "auto")
        if context_provider not in {"auto", "none", "ollama", "models"}:
            raise ConfigError("LLM_CONTEXT_PROVIDER: используйте auto, none, ollama или models.")

        transport = value("MCP_TRANSPORT", "stdio")
        if transport not in {"stdio", "streamable-http"}:
            raise ConfigError("MCP_TRANSPORT: используйте stdio или streamable-http.")

        def bounded(key: str, default: int, low: int, high: int) -> int:
            try:
                number = int(value(key, str(default)))
                if not low <= number <= high:
                    raise ValueError
                return number
            except ValueError:
                raise ConfigError(f"{key}: допустимый диапазон {low}..{high}.") from None

        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value("SCHEDULE_TIMEZONE", "Europe/Moscow"))
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigError("SCHEDULE_TIMEZONE: нужна зона IANA.") from None
        return cls(
            bot_token=token,
            postgres_password=password,
            telegram_proxy_url=proxy,
            postgres_host=value("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=port("POSTGRES_PORT", "5432"),
            postgres_db=value("POSTGRES_DB", "bot"),
            postgres_user=value("POSTGRES_USER", "bot"),
            log_level=level,
            health_port=port("HEALTH_PORT", "8080"),
            llm_api_key=llm_key,
            llm_base_url=llm_url,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            llm_max_tokens=bounded("LLM_MAX_TOKENS", 1024, 64, 16384),
            agent_timeout=bounded("AGENT_TIMEOUT", 240, 1, 1800),
            summary_max_chars=bounded("SUMMARY_MAX_CHARS", 2000, 100, 8000),
            tool_result_max_chars=bounded("TOOL_RESULT_MAX_CHARS", 12000, 100, 64000),
            llm_context_provider=context_provider,
            history_max_messages=max_messages,
            history_max_chars=max_chars,
            default_context_length=def_ctx,
            compaction_threshold_messages=compact_thresh,
            compaction_keep_recent=compact_keep,
            user_queue_max_size=queue_max,
            mcp_transport=transport,
            mcp_server_url=value("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp/"),
            mcp_shared_secret=value("MCP_SHARED_SECRET"),
            mcp_timeout=bounded("MCP_TIMEOUT", 10, 1, 120),
            agent_max_tool_calls=bounded("AGENT_MAX_TOOL_CALLS", 2, 1, 2),
            agent_max_llm_steps=bounded("AGENT_MAX_LLM_STEPS", 4, 1, 8),
            confirmation_ttl=bounded("CONFIRMATION_TTL", 300, 1, 300),
            schedule_path=value("SCHEDULE_PATH", "app/data/schedule.json"),
            schedule_timezone=value("SCHEDULE_TIMEZONE", "Europe/Moscow"),
        )
