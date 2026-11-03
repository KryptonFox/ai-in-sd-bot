import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock

import asyncpg
import httpx
from fastmcp import Client
from mcp import types

from app.core.clock import FrozenClock
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.server import build_server

_tools: list[types.Tool] = []
_resources: list[types.Resource] = []


async def _discover() -> None:
    settings = ServerSettings(
        postgres_password="unused",
        mcp_shared_secret="catalog-test-secret-not-for-production",
    )
    pool = cast(asyncpg.Pool, Mock(spec=asyncpg.Pool))
    clock = FrozenClock(datetime(2026, 10, 12, 9, tzinfo=UTC))

    async with httpx.AsyncClient() as http:
        async with Client(build_server(pool, settings, http, clock)) as client:
            _tools.extend(await client.list_tools())
            _resources.extend(await client.list_resources())


def initialize_catalog() -> None:
    if not _tools:
        asyncio.run(_discover())


def tool_catalog() -> list[types.Tool]:
    return [tool.model_copy(deep=True) for tool in _tools]


def resource_catalog() -> list[types.Resource]:
    return [resource.model_copy(deep=True) for resource in _resources]
