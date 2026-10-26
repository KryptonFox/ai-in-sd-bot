import argparse
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.db.connection import create_pool
from app.mcp_server.config import ServerSettings
from app.mcp_server.server import build_server


def http_app(server, secret: str, pool):
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def health(request):
        try:
            healthy = await pool.fetchval("SELECT 1", timeout=3) == 1
        except Exception:
            healthy = False
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable"}, status_code=200 if healthy else 503
        )

    async def endpoint(scope, receive, send):
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if not hmac.compare_digest(authorization, f"Bearer {secret}"):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    return Starlette(
        routes=[Route("/health", health), Mount("/mcp", app=endpoint)], lifespan=lifespan
    )


async def run(settings, transport):
    pool = await create_pool(settings)
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            server = build_server(pool, settings, http)
            if transport == "stdio":
                async with stdio_server() as (read, write):
                    await server.run(read, write, server.create_initialization_options())
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("mcp", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    settings = ServerSettings.load(args.env_file)
    asyncio.run(run(settings, args.transport or settings.mcp_transport))


if __name__ == "__main__":
    main()
