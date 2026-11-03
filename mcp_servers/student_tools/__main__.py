import argparse
import asyncio
import hmac
import logging

import asyncpg
import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import MCPTransport
from app.db.connection import create_pool
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server


def http_app(server: FastMCP, secret: str, pool: asyncpg.Pool) -> StarletteWithLifespan:
    app = server.http_app(path="/mcp", json_response=True, stateless_http=True)

    async def health(request: Request) -> JSONResponse:
        try:
            healthy = await pool.fetchval("SELECT 1", timeout=3) == 1
        except Exception:
            healthy = False
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable"}, status_code=200 if healthy else 503
        )

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self,
            request: Request,
            call_next: RequestResponseEndpoint,
        ) -> Response:
            if request.url.path == "/health":
                return await call_next(request)
            authorization = request.headers.get("authorization", "")
            if not hmac.compare_digest(authorization, f"Bearer {secret}"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    app.add_route("/health", health, methods=["GET"])
    return app


async def run(settings: ServerSettings, transport: MCPTransport) -> None:
    pool = await create_pool(settings)
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            server = build_server(pool, settings, http)
            if transport == MCPTransport.STDIO:
                await server.run_async(transport="stdio")
            else:
                app = http_app(server, settings.mcp_shared_secret, pool)
                config = uvicorn.Config(
                    app,
                    host=settings.mcp_host,
                    port=settings.mcp_port,
                    log_level="warning",
                    access_log=False,
                )
                await uvicorn.Server(config).serve()
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
