import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp.client.stdio import get_default_environment

from app.config import MCPTransport, Settings
from app.mcp.connection import (
    create_math_tools_client,
    create_student_tools_client,
    create_tool_clients,
)

SECRET = "shared-mcp-secret-0123456789abcdef"
TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


def stdio_settings(**changes: object) -> Settings:
    settings = Settings(
        bot_token=TOKEN,
        postgres_password="database-password",
        postgres_host="database-host",
        postgres_port=5544,
        postgres_db="student-tools",
        postgres_user="worker",
        mcp_transport=MCPTransport.STDIO,
        mcp_shared_secret=SECRET,
        schedule_path="/data/schedule.json",
        schedule_timezone="Europe/Moscow",
    )
    return replace(settings, **changes)


def test_stdio_factory_launches_both_servers_with_isolated_math_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "proxy-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "environment-database-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("HOME", "/tmp/student-home")

    clients = create_tool_clients(stdio_settings())
    student_transport = clients.student._client.transport

    assert clients.math is not None
    math_transport = clients.math._client.transport
    assert isinstance(student_transport, StdioTransport)
    assert isinstance(math_transport, StdioTransport)
    assert student_transport.args == [
        "-m",
        "mcp_servers.student_tools",
        "--transport",
        "stdio",
        "--env-file",
        os.devnull,
    ]
    assert math_transport.args == [
        "-m",
        "mcp_servers.math_tools",
        "--transport",
        "stdio",
        "--env-file",
        os.devnull,
    ]

    assert student_transport.env is not None
    assert math_transport.env is not None
    assert student_transport.env["MCP_SHARED_SECRET"] == SECRET
    assert student_transport.env["POSTGRES_PASSWORD"] == "database-password"
    assert math_transport.env["MCP_SHARED_SECRET"] == SECRET
    assert math_transport.env["HOME"] == "/tmp/student-home"

    forbidden = {
        "BOT_TOKEN",
        "LLM_API_KEY",
        "TELEGRAM_PROXY_URL",
        "DATABASE_URL",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    assert forbidden.isdisjoint(math_transport.env)
    assert clients.student.secret == clients.math.secret == SECRET
    assert len(clients.composite.clients) == 2


def test_stdio_factory_generates_one_secret_for_both_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/tmp/student-home")

    clients = create_tool_clients(stdio_settings(mcp_shared_secret=""))

    assert clients.math is not None
    assert clients.student.secret == clients.math.secret
    assert len(clients.student.secret) >= 32
    math_transport = clients.math._client.transport
    assert isinstance(math_transport, StdioTransport)
    assert math_transport.env is not None
    assert math_transport.env["MCP_SHARED_SECRET"] == clients.student.secret


def test_http_factory_uses_separate_urls_and_one_bearer_secret() -> None:
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        mcp_transport=MCPTransport.STREAMABLE_HTTP,
        mcp_shared_secret=SECRET,
        mcp_server_url="http://student-tools.test/mcp/",
        math_mcp_server_url="http://math-tools.test/mcp/",
    )

    clients = create_tool_clients(settings)
    student_transport = clients.student._client.transport
    assert clients.math is not None
    math_transport = clients.math._client.transport

    assert isinstance(student_transport, StreamableHttpTransport)
    assert isinstance(math_transport, StreamableHttpTransport)
    assert student_transport.url == "http://student-tools.test/mcp/"
    assert math_transport.url == "http://math-tools.test/mcp/"
    assert student_transport.headers == {"Authorization": f"Bearer {SECRET}"}
    assert math_transport.headers == {"Authorization": f"Bearer {SECRET}"}
    assert len(clients.composite.clients) == 2


def test_http_factory_with_empty_math_url_keeps_one_server_mode() -> None:
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        mcp_transport=MCPTransport.STREAMABLE_HTTP,
        mcp_shared_secret=SECRET,
        mcp_server_url="http://student-tools.test/mcp/",
        math_mcp_server_url="",
    )

    clients = create_tool_clients(settings)

    assert clients.math is None
    assert clients.composite.clients == (clients.student,)
    assert isinstance(clients.student._client.transport, StreamableHttpTransport)


def test_http_math_client_requires_shared_secret_only_when_configured() -> None:
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        mcp_transport=MCPTransport.STREAMABLE_HTTP,
        mcp_shared_secret="",
        mcp_server_url="http://student-tools.test/mcp/",
        math_mcp_server_url="",
    )

    assert create_math_tools_client(settings) is None
    with pytest.raises(ValueError, match="MCP secret is required"):
        create_math_tools_client(replace(settings, math_mcp_server_url="http://math.test/mcp/"))
    with pytest.raises(ValueError, match="MCP secret is required"):
        create_student_tools_client(settings)


def test_settings_load_math_server_url_and_preserve_single_server_default(tmp_path) -> None:
    environment = {
        "BOT_TOKEN": TOKEN,
        "POSTGRES_PASSWORD": "unused",
        "MATH_MCP_SERVER_URL": "  http://math-tools.test/mcp/  ",
    }

    configured = Settings.load(tmp_path / "missing.env", environ=environment)
    default = Settings.load(
        tmp_path / "missing.env",
        environ={**environment, "MATH_MCP_SERVER_URL": "  "},
    )

    assert configured.math_mcp_server_url == "http://math-tools.test/mcp/"
    assert default.math_mcp_server_url == ""


def test_math_subprocess_environment_after_sdk_defaults_has_no_application_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "proxy-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "environment-database-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("MCP_TRANSPORT", "http")

    clients = create_tool_clients(stdio_settings())
    assert clients.math is not None
    transport = clients.math._client.transport
    assert isinstance(transport, StdioTransport)

    child_environment = get_default_environment() | (transport.env or {})
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(sorted(os.environ)))",
        ],
        env=child_environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    child_keys = set(json.loads(probe.stdout))

    forbidden = {
        "BOT_TOKEN",
        "LLM_API_KEY",
        "TELEGRAM_PROXY_URL",
        "DATABASE_URL",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "MCP_TRANSPORT",
    }
    assert forbidden.isdisjoint(child_keys)
    assert "MCP_SHARED_SECRET" in child_keys
