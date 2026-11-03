import json
import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ResourceError
from mcp import MCPError, types

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.mcp import ResourceContent, ResourceDefinition
from app.core.schemas.tools import ToolFailure
from app.mcp.catalog import ToolCatalog

logger = logging.getLogger("app.mcp.client")


class MCPClient:
    def __init__(self, client: Client, *, secret: str = "") -> None:
        self._client = client
        self.secret = secret
        self.catalog = ToolCatalog()
        self.tools: dict[str, ToolDefinition] = {}
        self.resources: dict[str, ResourceDefinition] = {}
        self.available = False

    def load_catalog(self, catalog: Sequence[types.Tool]) -> None:
        self.tools = self.catalog.load(catalog)
        self.available = bool(self.tools or self.resources)

    def load_resources(self, catalog: Sequence[types.Resource]) -> None:
        resources = {
            str(item.uri): ResourceDefinition(
                uri=str(item.uri),
                name=item.name,
                description=item.description or "",
                mime_type=item.mime_type or "application/json",
            )
            for item in catalog
        }
        self.resources = resources
        self.available = bool(self.tools or resources)

    def is_read_only(self, name: str) -> bool:
        return self.catalog.is_read_only(name)

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        self.catalog.validate_arguments(name, arguments)

    async def start(self) -> bool:
        try:
            async with self._request() as client:
                tools = await client.list_tools()
                resources = await client.list_resources()
                self.load_catalog(tools)
                self.load_resources(resources)
        except ToolFailure:
            self.available = False
            return False

        logger.info("mcp_connected tools=%d resources=%d", len(self.tools), len(self.resources))
        return self.available

    @asynccontextmanager
    async def _request(self) -> AsyncGenerator[Client, None]:
        try:
            async with self._client:
                yield self._client
        except (ToolFailure, ResourceError):
            raise
        except Exception as error:
            logger.warning("mcp_request_failed code=%s", type(error).__name__)
            if isinstance(error, TimeoutError) or (
                isinstance(error, MCPError) and error.code == types.REQUEST_TIMEOUT
            ):
                raise ToolFailure("mcp_timeout", "Превышено время ожидания MCP-сервера.") from None
            self.available = False
            raise ToolFailure("mcp_unavailable", "MCP-сервер недоступен.") from None

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        self.catalog.validate_arguments(name, arguments)
        meta: dict[str, object] | None = None
        if not self.catalog.is_read_only(name):
            if user_id is None or confirmation_id is None:
                raise ToolFailure("confirmation_required", "Нужно подтверждение владельца.")
            meta = {
                "app_token": self.secret,
                "user_id": user_id,
                "confirmation_id": confirmation_id,
            }

        async with self._request() as client:
            result = await client.call_tool(
                name,
                dict(arguments),
                meta=meta,
                raise_on_error=False,
            )

        if result.is_error:
            raise self._tool_error(result)
        return self.catalog.validate_output(name, result.structured_content)

    @staticmethod
    def _tool_error(result: CallToolResult) -> ToolFailure:
        payload = result.structured_content
        if payload is None:
            for content in result.content:
                if isinstance(content, types.TextContent):
                    try:
                        decoded = json.loads(content.text)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(decoded, dict):
                        payload = decoded
                        break
        error = payload.get("error") if payload is not None else None
        code = error.get("code") if isinstance(error, dict) else None
        safe_code = (
            code if isinstance(code, str) and code.replace("_", "").isalnum() else "tool_failed"
        )
        return ToolFailure(safe_code)

    async def read_resource(self, uri: str) -> ResourceContent:
        if uri not in self.resources:
            raise ToolFailure("resource_unavailable", "Ресурс недоступен.")
        try:
            async with self._request() as client:
                contents = await client.read_resource(uri)
        except ResourceError:
            raise ToolFailure("resource_unavailable", "Ресурс сейчас недоступен.") from None

        try:
            if len(contents) != 1:
                raise ValueError("expected_single_resource_content")
            content = contents[0]
            if not isinstance(content, types.TextResourceContents):
                raise ValueError("expected_text_resource")
            if str(content.uri) != uri:
                raise ValueError("unexpected_resource_uri")
            return ResourceContent(
                uri=uri,
                text=content.text,
                mime_type=content.mime_type or self.resources[uri].mime_type,
            )
        except Exception:
            raise ToolFailure("invalid_output", "Ресурс вернул некорректный результат.") from None

    async def aclose(self) -> None:
        self.available = False
        try:
            await self._client.close()
        except Exception as error:
            await self._client.transport.close()
            logger.warning("mcp_close_failed code=%s", type(error).__name__)
