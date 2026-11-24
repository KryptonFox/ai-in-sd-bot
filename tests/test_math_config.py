import io
import json
import sys
from types import TracebackType

import pytest

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


def test_healthcheck_cli_returns_status_for_health_endpoint(monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(url: str, timeout: int) -> FakeResponse:
        calls.append(url)
        assert timeout == 5
        return FakeResponse(json.dumps({"status": "ok"}).encode("utf-8"))

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["healthcheck", "--port", "8123"])
    assert healthcheck.main() == 0
    assert calls == ["http://127.0.0.1:8123/health"]


def test_healthcheck_cli_returns_failure_for_bad_status(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: int) -> FakeResponse:
        return FakeResponse(json.dumps({"status": "unavailable"}).encode("utf-8"))

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["healthcheck", "--port", "8123"])
    assert healthcheck.main() == 1


def test_healthcheck_cli_returns_failure_when_unreachable(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: int) -> FakeResponse:
        raise OSError("unreachable")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["healthcheck", "--port", "8123"])
    assert healthcheck.main() == 1
