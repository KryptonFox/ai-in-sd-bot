import argparse
import asyncio
import hmac
import logging
import os

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.toml_config import DEFAULT_CONFIG_FILE
from mcp_servers.math_tools.config import MathConfigError, MathSettings, MathTransport
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.runtime import MathRuntime
from mcp_servers.math_tools.server import build_server
from mcp_servers.math_tools.service import MathService

logger = logging.getLogger("math_tools")


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

    runtime = MathRuntime(
        startup_timeout=settings.math_mcp_startup_timeout_seconds,
        operation_timeout=settings.math_mcp_operation_timeout_seconds,
        queue_timeout=settings.math_mcp_queue_timeout_seconds,
        memory_limit_mib=settings.math_mcp_memory_limit_mib,
    )
    service = MathService(runtime, max_png_bytes=settings.math_mcp_max_png_bytes)

    try:
        await runtime.warmup()
    except MathError as error:
        logger.warning("math_warmup_failed code=%s", error.code)

    server = build_server(service)
    if transport == MathTransport.STDIO:
        await server.run_async(transport="stdio", show_banner=False)
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
    logging.getLogger("math_tools").setLevel(logging.INFO)
    for name in ("mcp", "fastmcp", "httpx", "httpcore", "uvicorn"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    config_file = os.environ.get("MATH_MCP_CONFIG_FILE", DEFAULT_CONFIG_FILE)
    settings = MathSettings.load(args.env_file, config_file=config_file)
    transport = MathTransport(args.transport) if args.transport else settings.mcp_transport
    asyncio.run(run(settings, transport))


if __name__ == "__main__":
    main()
