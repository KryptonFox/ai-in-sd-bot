import argparse
import asyncio
import logging
import os

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_servers.http import authenticated_app, serve_http
from mcp_servers.math_tools.config import MathConfigError, MathSettings, MathTransport
from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.runtime import MathRuntime
from mcp_servers.math_tools.server import build_server
from mcp_servers.math_tools.service import MathService

logger = logging.getLogger("math_tools")


def http_app(server: FastMCP, secret: str) -> StarletteWithLifespan:
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return authenticated_app(server, secret, health)


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
    await serve_http(app, settings.mcp_host, settings.mcp_port)


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent mathematical MCP server")
    parser.add_argument("--transport", choices=[transport.value for transport in MathTransport])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("math_tools").setLevel(logging.INFO)
    for name in ("mcp", "fastmcp", "httpx", "httpcore", "uvicorn"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    config_file = os.environ.get("MATH_MCP_CONFIG_FILE") or None
    settings = MathSettings.load(args.env_file, config_file=config_file)
    transport = MathTransport(args.transport) if args.transport else settings.mcp_transport
    asyncio.run(run(settings, transport))


if __name__ == "__main__":
    main()
