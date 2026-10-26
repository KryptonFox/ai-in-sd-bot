import asyncio
import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, overload

import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from jsonschema import Draft202012Validator, FormatChecker
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage
from pydantic import AnyUrl

from app.config import Settings
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.tools import ToolFailure, WeekSchedule

ALLOWED_TOOLS = frozenset({"get_weather", "get_schedule", "add_reminder"})
logger = logging.getLogger("app.mcp.client")

type _CommandMethod = Literal["call_tool", "read_resource"]
type _CommandResult = types.CallToolResult | types.ReadResourceResult
type _TransportStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]


@dataclass(frozen=True)
class _Command:
    method: _CommandMethod
    name: str
    arguments: dict[str, object] | None
    meta: dict[str, object] | None
    future: asyncio.Future[_CommandResult]


class MCPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.secret = settings.mcp_shared_secret or secrets.token_urlsafe(32)
        self.tools: dict[str, ToolDefinition] = {}
        self.outputs: dict[str, dict[str, object]] = {}
        self.resources: set[str] = set()
        self.available = False
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._queue: asyncio.Queue[_Command] = asyncio.Queue()
        self._retry_at = 0.0

    def load_catalog(self, catalog: Sequence[types.Tool]) -> None:
        tools: dict[str, ToolDefinition] = {}
        outputs: dict[str, dict[str, object]] = {}

        for tool in catalog:
            if tool.name not in ALLOWED_TOOLS or tool.outputSchema is None:
                continue

            Draft202012Validator.check_schema(tool.inputSchema)
            Draft202012Validator.check_schema(tool.outputSchema)
            tools[tool.name] = ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                parameters=tool.inputSchema,
            )
            outputs[tool.name] = tool.outputSchema

        self.tools = tools
        self.outputs = outputs
        self.available = bool(tools)

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        if name not in self.tools:
            raise ToolFailure("unknown_tool", "Инструмент не разрешён или недоступен.")

        try:
            Draft202012Validator(
                self.tools[name].parameters,
                format_checker=FormatChecker(),
            ).validate(arguments)
        except Exception:
            raise ToolFailure("invalid_arguments", "Уточните параметры инструмента.") from None

    async def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return self.available

        if time.monotonic() < self._retry_at:
            return False

        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._worker())

        try:
            async with asyncio.timeout(self.settings.mcp_timeout):
                await self._ready.wait()
        except TimeoutError:
            logger.warning("mcp_start_timeout transport=%s", self.settings.mcp_transport)
            await self.aclose()

        return self.available

    async def _open_stdio(self, stack: AsyncExitStack) -> _TransportStreams:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"BOT_TOKEN", "LLM_API_KEY", "TELEGRAM_PROXY_URL"}
        }
        env.update(
            {
                "MCP_SHARED_SECRET": self.secret,
                "POSTGRES_HOST": self.settings.postgres_host,
                "POSTGRES_PORT": str(self.settings.postgres_port),
                "POSTGRES_DB": self.settings.postgres_db,
                "POSTGRES_USER": self.settings.postgres_user,
                "POSTGRES_PASSWORD": self.settings.postgres_password,
                "SCHEDULE_PATH": self.settings.schedule_path,
                "SCHEDULE_TIMEZONE": self.settings.schedule_timezone,
            }
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server", "--transport", "stdio", "--env-file", os.devnull],
            env=env,
        )
        return await stack.enter_async_context(stdio_client(parameters))

    async def _open_http(self, stack: AsyncExitStack) -> _TransportStreams:
        if not self.settings.mcp_shared_secret:
            raise ValueError("MCP secret is required")

        http = await stack.enter_async_context(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.secret}"},
                timeout=self.settings.mcp_timeout,
                follow_redirects=True,
            )
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(self.settings.mcp_server_url, http_client=http)
        )
        return read, write

    async def _worker(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.settings.mcp_transport == "stdio":
                    read, write = await self._open_stdio(stack)
                else:
                    read, write = await self._open_http(stack)

                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.settings.mcp_timeout),
                    )
                )
                await self._discover(session)
                await self._process_commands(session)
        except Exception as error:
            logger.warning(
                "mcp_connection_failed transport=%s code=%s",
                self.settings.mcp_transport,
                type(error).__name__,
            )
        finally:
            self.available = False
            self._retry_at = time.monotonic() + 5
            self._ready.set()
            self._fail_pending_commands()

    async def _discover(self, session: ClientSession) -> None:
        await session.initialize()
        self.load_catalog((await session.list_tools()).tools)
        self.resources = {str(item.uri) for item in (await session.list_resources()).resources}
        self._ready.set()
        logger.info(
            "mcp_connected transport=%s tools=%d",
            self.settings.mcp_transport,
            len(self.tools),
        )

    async def _process_commands(self, session: ClientSession) -> None:
        while True:
            command = await self._queue.get()
            if command.future.cancelled():
                continue

            try:
                result = await self._execute_command(session, command)
                if not command.future.done():
                    command.future.set_result(result)
            except Exception as error:
                logger.warning("mcp_command_failed code=%s", type(error).__name__)
                if not command.future.done():
                    command.future.set_exception(
                        ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")
                    )
                return

    @staticmethod
    async def _execute_command(session: ClientSession, command: _Command) -> _CommandResult:
        if command.method == "call_tool":
            return await session.call_tool(command.name, command.arguments, meta=command.meta)

        return await session.read_resource(AnyUrl(command.name))

    def _fail_pending_commands(self) -> None:
        while not self._queue.empty():
            command = self._queue.get_nowait()
            if not command.future.done():
                command.future.set_exception(
                    ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")
                )

    @overload
    async def _command(
        self,
        method: Literal["call_tool"],
        name: str,
        arguments: Mapping[str, object],
        *,
        meta: dict[str, object] | None = None,
    ) -> types.CallToolResult: ...

    @overload
    async def _command(
        self,
        method: Literal["read_resource"],
        name: str,
    ) -> types.ReadResourceResult: ...

    async def _command(
        self,
        method: _CommandMethod,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        meta: dict[str, object] | None = None,
    ) -> _CommandResult:
        if not self.available and not await self.start():
            raise ToolFailure("mcp_unavailable", "MCP-сервер сейчас недоступен.")

        future: asyncio.Future[_CommandResult] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            _Command(method, name, dict(arguments) if arguments is not None else None, meta, future)
        )

        try:
            async with asyncio.timeout(self.settings.mcp_timeout):
                return await future
        except TimeoutError:
            raise ToolFailure("mcp_timeout", "Превышено время ожидания MCP-сервера.") from None

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        self.validate_arguments(name, arguments)
        meta: dict[str, object] | None = None

        if name == "add_reminder":
            if user_id is None or confirmation_id is None:
                raise ToolFailure("confirmation_required", "Нужно подтверждение владельца.")

            meta = {
                "app_token": self.secret,
                "user_id": user_id,
                "confirmation_id": confirmation_id,
            }

        result = await self._command("call_tool", name, arguments, meta=meta)
        if result.isError:
            raise self._tool_error(result)

        return self._validated_output(name, result.structuredContent)

    @staticmethod
    def _tool_error(result: types.CallToolResult) -> ToolFailure:
        error = (result.structuredContent or {}).get("error")
        code = error.get("code") if isinstance(error, dict) else None
        safe_code = (
            code if isinstance(code, str) and code.replace("_", "").isalnum() else "tool_failed"
        )
        return ToolFailure(safe_code, "Инструмент не смог выполнить запрос.")

    def _validated_output(self, name: str, payload: object) -> dict[str, object]:
        try:
            if not isinstance(payload, dict) or "error" in payload:
                raise ValueError("missing_success_payload")

            json.dumps(payload, allow_nan=False)
            Draft202012Validator(
                self.outputs[name],
                format_checker=FormatChecker(),
            ).validate(payload)
        except Exception:
            raise ToolFailure(
                "invalid_output", "Инструмент вернул некорректный результат."
            ) from None

        return payload

    async def read_week(self) -> dict[str, object]:
        if "schedule://current-week" not in self.resources:
            raise ToolFailure("resource_unavailable", "Ресурс недели недоступен.")

        result = await self._command("read_resource", "schedule://current-week")

        try:
            content = result.contents[0]
            if not isinstance(content, types.TextResourceContents):
                raise ValueError("expected_text_resource")

            return WeekSchedule.model_validate_json(content.text).model_dump(mode="json")
        except Exception:
            raise ToolFailure("invalid_output", "Ресурс вернул некорректный результат.") from None

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        self.available = False
