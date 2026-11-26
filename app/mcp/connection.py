import os
import secrets
import sys
from dataclasses import dataclass, replace

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from app.config import WEB_SEARCH_ENVIRONMENT_KEYS, MCPTransport, Settings
from app.mcp.client import MCPClient
from app.mcp.composite import CompositeToolClient

MATH_STDIO_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "MPLCONFIGDIR",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)
STUDENT_STDIO_EXCLUDED_ENVIRONMENT_KEYS = frozenset(
    {
        "BOT_TOKEN",
        "LLM_API_KEY",
        "ROUTER_API_KEY",
        "TELEGRAM_PROXY_URL",
    }
)
STUDENT_STDIO_EXCLUDED_CREDENTIAL_PREFIXES = ("GOOGLE", "GEMINI")


@dataclass(frozen=True)
class ToolClients:
    student: MCPClient
    math: MCPClient | None
    composite: CompositeToolClient


def create_tool_clients(settings: Settings) -> ToolClients:
    secret = settings.mcp_shared_secret
    if settings.mcp_transport == MCPTransport.STDIO and not secret:
        secret = secrets.token_urlsafe(32)

    configured = replace(settings, mcp_shared_secret=secret)
    student = create_student_tools_client(configured)
    math = create_math_tools_client(configured)

    clients = [student]
    if math is not None:
        clients.append(math)

    return ToolClients(
        student=student,
        math=math,
        composite=CompositeToolClient(clients),
    )


def create_student_tools_client(settings: Settings) -> MCPClient:
    secret = settings.mcp_shared_secret or secrets.token_urlsafe(32)
    transport: StdioTransport | StreamableHttpTransport

    if settings.mcp_transport == MCPTransport.STREAMABLE_HTTP:
        if not settings.mcp_shared_secret:
            raise ValueError("MCP secret is required")

        transport = StreamableHttpTransport(
            settings.mcp_server_url,
            headers={"Authorization": f"Bearer {secret}"},
        )
    else:
        env = _student_stdio_environment(settings, secret)
        transport = StdioTransport(
            command=sys.executable,
            args=[
                "-m",
                "mcp_servers.student_tools",
                "--transport",
                "stdio",
                "--env-file",
                os.devnull,
            ],
            env=env,
            keep_alive=True,
        )

    sdk = Client(
        transport,
        name="study-bot",
        timeout=settings.mcp_timeout,
        init_timeout=settings.mcp_timeout,
    )
    return MCPClient(sdk, secret=secret)


def _student_stdio_environment(settings: Settings, secret: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in STUDENT_STDIO_EXCLUDED_ENVIRONMENT_KEYS
        and key not in WEB_SEARCH_ENVIRONMENT_KEYS
        and not key.upper().startswith(STUDENT_STDIO_EXCLUDED_CREDENTIAL_PREFIXES)
    }
    env.update(
        {
            "MCP_SHARED_SECRET": secret,
            "POSTGRES_HOST": settings.postgres_host,
            "POSTGRES_PORT": str(settings.postgres_port),
            "POSTGRES_DB": settings.postgres_db,
            "POSTGRES_USER": settings.postgres_user,
            "POSTGRES_PASSWORD": settings.postgres_password,
            "SCHEDULE_PATH": settings.schedule_path,
            "SCHEDULE_TIMEZONE": settings.schedule_timezone,
        }
    )
    env.update(
        {
            key: value
            for key, value in settings.web_search_environment.items()
            if key in WEB_SEARCH_ENVIRONMENT_KEYS
        }
    )
    return env


def create_math_tools_client(settings: Settings) -> MCPClient | None:
    if settings.mcp_transport == MCPTransport.STREAMABLE_HTTP:
        if not settings.math_mcp_server_url:
            return None
        if not settings.mcp_shared_secret:
            raise ValueError("MCP secret is required")

        transport: StdioTransport | StreamableHttpTransport = StreamableHttpTransport(
            settings.math_mcp_server_url,
            headers={"Authorization": f"Bearer {settings.mcp_shared_secret}"},
        )
        secret = settings.mcp_shared_secret
    else:
        secret = settings.mcp_shared_secret or secrets.token_urlsafe(32)
        env = {
            key: value for key, value in os.environ.items() if key in MATH_STDIO_ENVIRONMENT_KEYS
        }
        env.update(settings.math_mcp_environment)
        env["MCP_SHARED_SECRET"] = secret
        transport = StdioTransport(
            command=sys.executable,
            args=[
                "-m",
                "mcp_servers.math_tools",
                "--transport",
                "stdio",
                "--env-file",
                os.devnull,
            ],
            env=env,
            keep_alive=True,
        )

    math_timeout = settings.math_mcp_request_timeout_seconds
    sdk = Client(
        transport,
        name="study-bot",
        timeout=math_timeout,
        init_timeout=math_timeout,
    )
    return MCPClient(sdk, secret=secret)
