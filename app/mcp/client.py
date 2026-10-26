import asyncio
import json
import logging
import os
import secrets
import sys
import time
from contextlib import AsyncExitStack
from datetime import timedelta

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.tools import ToolFailure

ALLOWED_TOOLS = frozenset({"get_weather", "get_schedule", "add_reminder"})
logger = logging.getLogger("app.mcp.client")


class MCPClient:
    def __init__(self, settings):
        self.settings = settings
        self.secret = settings.mcp_shared_secret or secrets.token_urlsafe(32)
        self.tools: dict[str, ToolDefinition] = {}
        self.outputs = {}
        self.resources = set()
        self.available = False
        self._task = None
        self._ready = asyncio.Event()
        self._queue = asyncio.Queue()
        self._retry_at = 0.0

    def load_catalog(self, catalog):
        tools, outputs = {}, {}
        for tool in catalog:
            if tool.name not in ALLOWED_TOOLS or tool.outputSchema is None:
                continue
            Draft202012Validator.check_schema(tool.inputSchema)
            Draft202012Validator.check_schema(tool.outputSchema)
            tools[tool.name] = ToolDefinition(
                name=tool.name, description=tool.description or "", parameters=tool.inputSchema
            )
            outputs[tool.name] = tool.outputSchema
        self.tools, self.outputs = tools, outputs
        self.available = bool(tools)

    def validate_arguments(self, name, arguments):
        if name not in self.tools:
            raise ToolFailure("unknown_tool", "Инструмент не разрешён или недоступен.")
        try:
            Draft202012Validator(
                self.tools[name].parameters, format_checker=FormatChecker()
            ).validate(arguments)
        except Exception:
            raise ToolFailure("invalid_arguments", "Уточните параметры инструмента.") from None

    async def start(self):
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

    async def _worker(self):
        try:
            async with AsyncExitStack() as stack:
                if self.settings.mcp_transport == "stdio":
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
                        args=[
                            "-m",
                            "app.mcp_server",
                            "--transport",
                            "stdio",
                            "--env-file",
                            os.devnull,
                        ],
                        env=env,
                    )
                    read, write = await stack.enter_async_context(stdio_client(parameters))
                else:
                    if not self.settings.mcp_shared_secret:
                        raise ValueError("MCP secret is required")
                    http = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers={"Authorization": f"Bearer {self.secret}"},
                            timeout=self.settings.mcp_timeout,
                            follow_redirects=True,
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(self.settings.mcp_server_url, http_client=http)
                    )
                    read, write = streams[:2]
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.settings.mcp_timeout),
                    )
                )
                await session.initialize()
                self.load_catalog((await session.list_tools()).tools)
                self.resources = {
                    str(item.uri) for item in (await session.list_resources()).resources
                }
                self._ready.set()
                logger.info(
                    "mcp_connected transport=%s tools=%d",
                    self.settings.mcp_transport,
                    len(self.tools),
                )
                while True:
                    method, args, kwargs, future = await self._queue.get()
                    if future.cancelled():
                        continue
                    try:
                        value = await getattr(session, method)(*args, **kwargs)
                        if not future.done():
                            future.set_result(value)
                    except Exception as error:
                        logger.warning("mcp_command_failed code=%s", type(error).__name__)
                        if not future.done():
                            future.set_exception(
                                ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")
                            )
                        break
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
            while not self._queue.empty():
                *_, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(ToolFailure("mcp_unavailable", "MCP-сервер недоступен."))

    async def _command(self, method, *args, **kwargs):
        if not self.available and not await self.start():
            raise ToolFailure("mcp_unavailable", "MCP-сервер сейчас недоступен.")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put((method, args, kwargs, future))
        try:
            async with asyncio.timeout(self.settings.mcp_timeout):
                return await future
        except TimeoutError:
            raise ToolFailure("mcp_timeout", "Превышено время ожидания MCP-сервера.") from None

    async def call_tool(self, name, arguments, *, user_id=None, confirmation_id=None):
        self.validate_arguments(name, arguments)
        meta = None
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
            code = (result.structuredContent or {}).get("error", {}).get("code", "tool_failed")
            safe = (
                code if isinstance(code, str) and code.replace("_", "").isalnum() else "tool_failed"
            )
            raise ToolFailure(safe, "Инструмент не смог выполнить запрос.")
        payload = result.structuredContent
        try:
            if not isinstance(payload, dict) or "error" in payload:
                raise ValueError("missing_success_payload")
            json.dumps(payload, allow_nan=False)
            Draft202012Validator(self.outputs[name], format_checker=FormatChecker()).validate(
                payload
            )
        except Exception:
            raise ToolFailure(
                "invalid_output", "Инструмент вернул некорректный результат."
            ) from None
        return payload

    async def read_week(self):
        if "schedule://current-week" not in self.resources:
            raise ToolFailure("resource_unavailable", "Ресурс недели недоступен.")
        result = await self._command("read_resource", "schedule://current-week")
        from app.core.schemas.tools import WeekSchedule

        try:
            return WeekSchedule.model_validate_json(result.contents[0].text).model_dump(mode="json")
        except Exception:
            raise ToolFailure("invalid_output", "Ресурс вернул некорректный результат.") from None

    async def aclose(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.available = False
