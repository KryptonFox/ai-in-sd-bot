import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from app.core.ports import ToolClient
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.mcp import ResourceContent, ResourceDefinition
from app.core.schemas.tools import PhotoToolResult, ToolFailure

logger = logging.getLogger("app.mcp.composite")

TOOL_COLLISION_MESSAGE = "Имя инструмента совпадает у нескольких MCP-серверов."
RESOURCE_COLLISION_MESSAGE = "URI ресурса совпадает у нескольких MCP-серверов."


class ManagedToolClient(ToolClient, Protocol):
    async def start(self) -> bool: ...

    async def aclose(self) -> None: ...


def _unique_entries[T](catalogs: Sequence[Mapping[str, T]]) -> dict[str, T]:
    candidates: dict[str, list[T]] = {}

    for catalog in catalogs:
        for key, value in catalog.items():
            candidates.setdefault(key, []).append(value)

    return {key: values[0] for key, values in candidates.items() if len(values) == 1}


class CompositeToolClient(ToolClient):
    def __init__(self, clients: Sequence[ManagedToolClient]) -> None:
        self._clients = tuple(clients)

    @property
    def clients(self) -> tuple[ManagedToolClient, ...]:
        return self._clients

    @property
    def available(self) -> bool:
        return any(client.available for client in self._clients)

    @property
    def tools(self) -> Mapping[str, ToolDefinition]:
        available = [client for client in self._clients if client.available]
        return _unique_entries([client.tools for client in available])

    @property
    def resources(self) -> Mapping[str, ResourceDefinition]:
        available = [client for client in self._clients if client.available]
        return _unique_entries([client.resources for client in available])

    def is_read_only(self, name: str) -> bool:
        owners = self._tool_owners(name)
        if len(owners) != 1:
            return False
        return owners[0].is_read_only(name)

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        owner = self._resolve_tool(name)
        owner.validate_arguments(name, arguments)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object] | PhotoToolResult:
        owner = self._resolve_tool(name)
        return await owner.call_tool(
            name,
            arguments,
            user_id=user_id,
            confirmation_id=confirmation_id,
        )

    async def read_resource(self, uri: str) -> ResourceContent:
        owners = self._resource_owners(uri)
        if len(owners) > 1:
            raise ToolFailure("capability_conflict", RESOURCE_COLLISION_MESSAGE)
        if not owners:
            raise ToolFailure("resource_unavailable", "Ресурс недоступен.")

        return await owners[0].read_resource(uri)

    async def start(self) -> bool:
        results = await asyncio.gather(
            *(self._start(client) for client in self._clients),
        )
        return any(results)

    async def recover(self) -> bool:
        unavailable = tuple(client for client in self._clients if not client.available)
        if not unavailable:
            return False

        results = await asyncio.gather(
            *(self._start(client) for client in unavailable),
        )
        return any(results)

    async def aclose(self) -> None:
        await asyncio.gather(*(self._close(client) for client in self._clients))

    def _resolve_tool(self, name: str) -> ManagedToolClient:
        owners = self._tool_owners(name)
        if len(owners) > 1:
            raise ToolFailure("capability_conflict", TOOL_COLLISION_MESSAGE)
        if not owners:
            raise ToolFailure("unknown_tool", "Инструмент не разрешён или недоступен.")

        return owners[0]

    def _tool_owners(self, name: str) -> list[ManagedToolClient]:
        return [client for client in self._clients if client.available and name in client.tools]

    def _resource_owners(self, uri: str) -> list[ManagedToolClient]:
        return [client for client in self._clients if client.available and uri in client.resources]

    async def _start(self, client: ManagedToolClient) -> bool:
        try:
            await client.start()
        except Exception as error:
            logger.warning("mcp_composite_start_failed code=%s", type(error).__name__)
            return False

        return client.available

    async def _close(self, client: ManagedToolClient) -> None:
        try:
            await client.aclose()
        except Exception as error:
            logger.warning("mcp_composite_close_failed code=%s", type(error).__name__)
