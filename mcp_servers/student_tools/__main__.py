import argparse
import asyncio
import logging

import asyncpg
import httpx
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import MCPTransport
from app.db.connection import create_pool
from mcp_servers.http import authenticated_app, serve_http
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server


def http_app(server: FastMCP, secret: str, pool: asyncpg.Pool) -> StarletteWithLifespan:
    async def health(request: Request) -> JSONResponse:
        try:
            healthy = await pool.fetchval("SELECT 1", timeout=3) == 1
        except Exception:
            healthy = False
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable"}, status_code=200 if healthy else 503
        )

    return authenticated_app(server, secret, health)


async def run(settings: ServerSettings, transport: MCPTransport) -> None:
    pool = await create_pool(settings)
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            server = build_server(pool, settings, http)
            if transport == MCPTransport.STDIO:
                await server.run_async(transport="stdio")
            else:
                app = http_app(server, settings.mcp_shared_secret, pool)
                await serve_http(app, settings.mcp_host, settings.mcp_port)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=[transport.value for transport in MCPTransport])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("mcp", "fastmcp", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    settings = ServerSettings.load(args.env_file)
    transport = MCPTransport(args.transport) if args.transport else settings.mcp_transport
    asyncio.run(run(settings, transport))


if __name__ == "__main__":
    main()
