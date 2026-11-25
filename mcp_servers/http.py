import hmac
from collections.abc import Awaitable, Callable

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def authenticated_app(
    server: FastMCP,
    secret: str,
    health: Callable[[Request], Awaitable[Response]],
) -> StarletteWithLifespan:
    app = server.http_app(path="/mcp", json_response=True, stateless_http=True)
    bearer_token = f"Bearer {secret}".encode()

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            if request.url.path != "/health":
                authorization = request.headers.get("authorization", "").encode("latin-1")
                if not hmac.compare_digest(authorization, bearer_token):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)

            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    app.add_route("/health", health, methods=["GET"])
    return app


async def serve_http(app: StarletteWithLifespan, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    await uvicorn.Server(config).serve()
