import os
from dataclasses import replace

import pytest
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from app.config import MCPTransport, Settings
from app.mcp.connection import create_student_tools_client


def test_stdio_factory_sets_server_environment_and_isolates_application_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "proxy-secret")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    settings = Settings(
        bot_token="configured-bot-token",
        postgres_password="database-password",
        postgres_host="database-host",
        postgres_port=5544,
        postgres_db="student-tools",
        postgres_user="worker",
        mcp_transport=MCPTransport.STDIO,
        mcp_shared_secret="stdio-shared-secret",
        schedule_path="/data/schedule.json",
        schedule_timezone="Europe/Moscow",
    )

    client = create_student_tools_client(settings)
    transport = client._client.transport

    assert isinstance(transport, StdioTransport)
    assert transport.args == [
        "-m",
        "mcp_servers.student_tools",
        "--transport",
        "stdio",
        "--env-file",
        os.devnull,
    ]
    assert transport.env is not None
    assert transport.env["MCP_SHARED_SECRET"] == "stdio-shared-secret"
    assert transport.env["POSTGRES_HOST"] == "database-host"
    assert transport.env["POSTGRES_PORT"] == "5544"
    assert transport.env["POSTGRES_DB"] == "student-tools"
    assert transport.env["POSTGRES_USER"] == "worker"
    assert transport.env["POSTGRES_PASSWORD"] == "database-password"
    assert transport.env["SCHEDULE_PATH"] == "/data/schedule.json"
    assert transport.env["SCHEDULE_TIMEZONE"] == "Europe/Moscow"
    assert transport.env.get("BOT_TOKEN") is None
    assert transport.env.get("LLM_API_KEY") is None
    assert transport.env.get("TELEGRAM_PROXY_URL") is None
    assert transport.env["MCP_TRANSPORT"] == "http"


def test_http_factory_sets_bearer_secret_and_requires_it() -> None:
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        mcp_transport=MCPTransport.STREAMABLE_HTTP,
        mcp_shared_secret="http-shared-secret",
        mcp_server_url="http://mcp.test/mcp/",
    )

    client = create_student_tools_client(settings)

    assert isinstance(client._client.transport, StreamableHttpTransport)
    assert client._client.transport.headers == {"Authorization": "Bearer http-shared-secret"}

    missing_secret = replace(settings, mcp_shared_secret="")
    with pytest.raises(ValueError, match="MCP secret is required"):
        create_student_tools_client(missing_secret)
