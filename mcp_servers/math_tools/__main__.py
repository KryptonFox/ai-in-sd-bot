import argparse
import asyncio
import hmac
import logging

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_servers.math_tools.config import MathConfigError, MathSettings, MathTransport
from mcp_servers.math_tools.server import build_server


def http_app(server: FastMCP, secret: str) -> StarletteWithLifespan:
    app = server.http_app(path="/mcp", json_response=True, stateless_http=True)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

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


async def run(settings: MathSettings, transport: MathTransport) -> None:
    if transport == MathTransport.STREAMABLE_HTTP and not settings.mcp_shared_secret:
        raise MathConfigError("MCP_SHARED_SECRET: для HTTP нужен Bearer-секрет.")

    server = build_server()
    if transport == MathTransport.STDIO:
        await server.run_async(transport="stdio")
        return

    app = http_app(server, settings.mcp_shared_secret)
    config = uvicorn.Config(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="warning",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent mathematical MCP server")
    parser.add_argument("--transport", choices=[transport.value for transport in MathTransport])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    for name in ("mcp", "fastmcp", "httpx", "httpcore", "uvicorn"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    settings = MathSettings.load(args.env_file)
    transport = MathTransport(args.transport) if args.transport else settings.mcp_transport
    asyncio.run(run(settings, transport))


if __name__ == "__main__":
    main()
