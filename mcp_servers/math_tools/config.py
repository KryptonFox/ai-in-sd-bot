import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values


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

    @classmethod
    def load(
        cls,
        env_file: Path | str = ".env",
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "MathSettings":
        values = {
            **dotenv_values(env_file, interpolate=False),
            **(os.environ if environ is None else environ),
        }

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
        )
