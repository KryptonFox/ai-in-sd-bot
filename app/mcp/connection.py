import os
import secrets
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from app.config import MCPTransport, Settings
from app.mcp.client import MCPClient


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
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"BOT_TOKEN", "LLM_API_KEY", "TELEGRAM_PROXY_URL"}
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
