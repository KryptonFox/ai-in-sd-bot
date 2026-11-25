import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace, TracebackType

import pytest

from app.config import Settings
from app.core import health_probe
from app.core.schemas.tools import ToolFailure
from app.mcp.connection import create_math_tools_client
from mcp_servers.math_tools import healthcheck
from mcp_servers.math_tools.config import MathConfigError, MathSettings, MathTransport

SECRET = "math-config-secret-0123456789abcdef"


def test_settings_load_env_file_and_environment_override(tmp_path) -> None:
    env_file = tmp_path / "math.env"
    env_file.write_text(
        "\n".join(
            [
                f"MCP_SHARED_SECRET={SECRET}",
                "MCP_HOST=127.0.0.1",
                "MCP_PORT=8100",
                "MCP_TRANSPORT=streamable-http",
            ]
        ),
        encoding="utf-8",
    )

    loaded = MathSettings.load(env_file, environ={})
    assert loaded.mcp_shared_secret == SECRET
    assert loaded.mcp_host == "127.0.0.1"
    assert loaded.mcp_port == 8100
    assert loaded.mcp_transport == MathTransport.STREAMABLE_HTTP

    overridden = MathSettings.load(env_file, environ={"MCP_PORT": "8200"})
    assert overridden.mcp_port == 8200
    assert overridden.mcp_transport == MathTransport.STREAMABLE_HTTP


def test_stdio_settings_do_not_require_a_secret(tmp_path) -> None:
    settings = MathSettings.load(tmp_path / "missing.env", environ={})
    assert settings.mcp_shared_secret == ""
    assert settings.mcp_transport == MathTransport.STDIO
    assert settings.mcp_port == 8000


def test_http_settings_require_a_long_secret(tmp_path) -> None:
    with pytest.raises(MathConfigError):
        MathSettings.load(
            tmp_path / "missing.env",
            environ={"MCP_TRANSPORT": "streamable-http"},
        )
    with pytest.raises(MathConfigError):
        MathSettings.load(
            tmp_path / "missing.env",
            environ={"MCP_TRANSPORT": "streamable-http", "MCP_SHARED_SECRET": "short"},
        )


def test_settings_reject_invalid_transport_and_port(tmp_path) -> None:
    with pytest.raises(MathConfigError):
        MathSettings.load(tmp_path / "missing.env", environ={"MCP_TRANSPORT": "sse"})
    with pytest.raises(MathConfigError):
        MathSettings.load(tmp_path / "missing.env", environ={"MCP_PORT": "0"})
    with pytest.raises(MathConfigError):
        MathSettings.load(tmp_path / "missing.env", environ={"MCP_PORT": "text"})


async def test_stdio_enforces_png_limit_from_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MATH_MCP_MAX_PNG_BYTES=1024\n", encoding="utf-8")
    settings = Settings.load(
        env_file,
        environ={
            "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            "POSTGRES_PASSWORD": "test-password",
            "MCP_TRANSPORT": "stdio",
        },
    )
    client = create_math_tools_client(settings)
    assert client is not None

    try:
        assert await client.start()
        with pytest.raises(ToolFailure) as error:
            await client.call_tool("math_plot", {"functions": ["x^2"]})
        assert error.value.code == "resource_limit"
    finally:
        await client.aclose()


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._buffer.close()


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"status": "ok"}, 0),
        ({"status": "unavailable"}, 1),
        ([], 1),
        (None, 1),
    ],
)
def test_healthcheck_cli(monkeypatch: pytest.MonkeyPatch, payload: object, status: int) -> None:
    calls: list[str] = []

    def open_health_endpoint(url: str, timeout: int) -> FakeResponse:
        calls.append(url)
        assert timeout == 5
        if payload is None:
            raise OSError("unreachable")
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(
        health_probe, "build_opener", lambda _: SimpleNamespace(open=open_health_endpoint)
    )
    monkeypatch.setattr(sys, "argv", ["healthcheck", "--port", "8123"])

    assert healthcheck.main() == status
    assert calls == ["http://127.0.0.1:8123/health"]
