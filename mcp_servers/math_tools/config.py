import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

from app.core.toml_config import (
    DEFAULT_CONFIG_FILE,
    TomlConfigError,
    load_toml_environment,
    merge_environment,
)


class MathConfigError(ValueError):
    pass


class MathTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


@dataclass(frozen=True)
class MathSettings:
    mcp_shared_secret: str = field(default="", repr=False)
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_transport: MathTransport = MathTransport.STDIO
    math_mcp_startup_timeout_seconds: float = 30.0
    math_mcp_operation_timeout_seconds: float = 5.0
    math_mcp_queue_timeout_seconds: float = 5.0
    math_mcp_memory_limit_mib: int = 768
    math_mcp_max_png_bytes: int = 256 * 1024

    @classmethod
    def load(
        cls,
        env_file: Path | str = ".env",
        *,
        environ: Mapping[str, str] | None = None,
        config_file: Path | str = DEFAULT_CONFIG_FILE,
    ) -> "MathSettings":
        try:
            toml_values = load_toml_environment(config_file)
        except TomlConfigError as error:
            raise MathConfigError(str(error)) from None

        values = merge_environment(
            toml_values,
            dotenv_values(env_file, interpolate=False),
            os.environ if environ is None else environ,
        )

        def value(key: str, default: str = "") -> str:
            return values.get(key) or default

        def port() -> int:
            raw = value("MCP_PORT", "8000")
            try:
                parsed = int(raw)
            except ValueError:
                raise MathConfigError("MCP_PORT: нужен номер порта.") from None
            if not 1 <= parsed <= 65535:
                raise MathConfigError("MCP_PORT: нужен номер порта от 1 до 65535.")
            return parsed

        def seconds(key: str, default: float, low: float, high: float) -> float:
            try:
                parsed = float(value(key, str(default)))
                if not math.isfinite(parsed) or not low <= parsed <= high:
                    raise ValueError
                return parsed
            except ValueError:
                raise MathConfigError(f"{key}: допустимый диапазон {low:g}..{high:g}.") from None

        def bounded_integer(key: str, default: int, low: int, high: int) -> int:
            try:
                parsed = int(value(key, str(default)))
                if not low <= parsed <= high:
                    raise ValueError
                return parsed
            except ValueError:
                raise MathConfigError(f"{key}: допустимый диапазон {low}..{high}.") from None

        try:
            transport = MathTransport(value("MCP_TRANSPORT", "stdio"))
        except ValueError:
            raise MathConfigError("MCP_TRANSPORT: используйте stdio или streamable-http.") from None

        secret = value("MCP_SHARED_SECRET")
        if secret and len(secret) < 32:
            raise MathConfigError("MCP_SHARED_SECRET: задайте секрет не короче 32 символов.")
        if transport == MathTransport.STREAMABLE_HTTP and not secret:
            raise MathConfigError("MCP_SHARED_SECRET: для HTTP нужен Bearer-секрет.")

        return cls(
            mcp_shared_secret=secret,
            mcp_host=value("MCP_HOST", "0.0.0.0"),
            mcp_port=port(),
            mcp_transport=transport,
            math_mcp_startup_timeout_seconds=seconds(
                "MATH_MCP_STARTUP_TIMEOUT_SECONDS",
                30.0,
                1,
                120,
            ),
            math_mcp_operation_timeout_seconds=seconds(
                "MATH_MCP_OPERATION_TIMEOUT_SECONDS",
                5.0,
                0.1,
                60,
            ),
            math_mcp_queue_timeout_seconds=seconds(
                "MATH_MCP_QUEUE_TIMEOUT_SECONDS",
                5.0,
                0.1,
                180,
            ),
            math_mcp_memory_limit_mib=bounded_integer(
                "MATH_MCP_MEMORY_LIMIT_MIB",
                768,
                128,
                4096,
            ),
            math_mcp_max_png_bytes=bounded_integer(
                "MATH_MCP_MAX_PNG_BYTES",
                256 * 1024,
                1024,
                256 * 1024,
            ),
        )
