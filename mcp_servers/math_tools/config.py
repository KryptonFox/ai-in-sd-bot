import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from app.core.toml_config import (
    ConfigValueError,
    MCPTransport,
    environment_value,
    load_toml_environment,
    merge_environment,
    parse_choice,
    parse_integer,
    parse_number,
    parse_port,
)

MathTransport = MCPTransport


class MathConfigError(ConfigValueError):
    pass


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
        config_file: Path | str | None = None,
    ) -> "MathSettings":
        try:
            return _load_math_settings(env_file, environ=environ, config_file=config_file)
        except MathConfigError:
            raise
        except ConfigValueError as error:
            raise MathConfigError(str(error)) from None


def _math_config_path(
    config_file: Path | str | None,
    file_values: Mapping[str, str | None],
    process_values: Mapping[str, str],
) -> Path | None:
    if config_file is not None:
        return Path(config_file)

    value = environment_value(process_values, "MATH_MCP_CONFIG_FILE") or environment_value(
        file_values,
        "MATH_MCP_CONFIG_FILE",
    )
    return Path(value) if value else None


def _load_math_settings(
    env_file: Path | str,
    *,
    environ: Mapping[str, str] | None,
    config_file: Path | str | None,
) -> MathSettings:
    file_values = dotenv_values(env_file, interpolate=False)
    process_values = os.environ if environ is None else environ
    config_path = _math_config_path(config_file, file_values, process_values)

    toml_values = load_toml_environment(config_path)
    values = merge_environment(toml_values, file_values, process_values)

    secret = environment_value(values, "MCP_SHARED_SECRET")
    if secret and len(secret) < 32:
        raise MathConfigError("MCP_SHARED_SECRET: задайте секрет не короче 32 символов.")

    transport = MathTransport(
        parse_choice(
            "MCP_TRANSPORT",
            environment_value(values, "MCP_TRANSPORT", MathTransport.STDIO.value),
        )
    )
    if transport == MathTransport.STREAMABLE_HTTP and not secret:
        raise MathConfigError("MCP_SHARED_SECRET: для HTTP нужен Bearer-секрет.")

    return MathSettings(
        mcp_shared_secret=secret,
        mcp_host=environment_value(values, "MCP_HOST", "0.0.0.0"),
        mcp_port=parse_port(
            "MCP_PORT",
            environment_value(values, "MCP_PORT", "8000"),
        ),
        mcp_transport=transport,
        math_mcp_startup_timeout_seconds=parse_number(
            "MATH_MCP_STARTUP_TIMEOUT_SECONDS",
            environment_value(values, "MATH_MCP_STARTUP_TIMEOUT_SECONDS", "30.0"),
        ),
        math_mcp_operation_timeout_seconds=parse_number(
            "MATH_MCP_OPERATION_TIMEOUT_SECONDS",
            environment_value(values, "MATH_MCP_OPERATION_TIMEOUT_SECONDS", "5.0"),
        ),
        math_mcp_queue_timeout_seconds=parse_number(
            "MATH_MCP_QUEUE_TIMEOUT_SECONDS",
            environment_value(values, "MATH_MCP_QUEUE_TIMEOUT_SECONDS", "5.0"),
        ),
        math_mcp_memory_limit_mib=parse_integer(
            "MATH_MCP_MEMORY_LIMIT_MIB",
            environment_value(values, "MATH_MCP_MEMORY_LIMIT_MIB", "768"),
        ),
        math_mcp_max_png_bytes=parse_integer(
            "MATH_MCP_MAX_PNG_BYTES",
            environment_value(values, "MATH_MCP_MAX_PNG_BYTES", str(256 * 1024)),
        ),
    )
