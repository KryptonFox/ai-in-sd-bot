import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar

from pydantic import ValidationError

from app.core.limits import DEFAULT_AGENT_MAX_TOOL_CALLS
from app.core.ports import ToolClient, TurnStore
from app.core.schemas.agent import AgentResult
from app.core.schemas.execution import RunState, StepAction
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.mcp import ReadResourceArguments, ResourceContent, ResourceDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import (
    MAX_AGENT_IMAGE_BYTES,
    MAX_AGENT_IMAGES,
    PhotoToolResult,
    ToolFailure,
    ToolImage,
)
from app.services.confirmation_service import ConfirmationService

logger = logging.getLogger("app.tools")
REMINDER_TOOL_NAME = "add_reminder"
READ_RESOURCE_FUNCTION = "read_resource"
REQUEST_DEADLINE_MESSAGE = "Общее время обработки запроса истекло."
T = TypeVar("T")

type ValidatedCall = tuple[ToolCall, ReadResourceArguments | None]


class ToolExecutionService:
    def __init__(
        self,
        mcp: ToolClient | None = None,
        confirmations: ConfirmationService | None = None,
        store: TurnStore | None = None,
        *,
        max_calls: int = DEFAULT_AGENT_MAX_TOOL_CALLS,
        max_output_chars: int = 12000,
    ) -> None:
        self._mcp = mcp
        self._confirmations = confirmations
        self._store = store
        self._max_calls = max_calls
        self._max_output_chars = max_output_chars

    @property
    def available(self) -> bool:
        return self._mcp is not None and self._mcp.available

    @property
    def definitions(self) -> list[ToolDefinition]:
        if not self.available or self._mcp is None:
            return []
        return list(self._mcp.tools.values())

    @property
    def resources(self) -> Mapping[str, ResourceDefinition]:
        if not self.available or self._mcp is None:
            return {}
        return self._mcp.resources

    def _client(self) -> ToolClient:
        if self._mcp is None or not self._mcp.available:
            raise ToolFailure("mcp_unavailable", "MCP-сервер недоступен.")
        return self._mcp

    def _check_budget(self, state: RunState) -> None:
        if state.calls_used >= self._max_calls:
            raise ToolFailure("call_budget", "Достигнут лимит вызовов MCP.")

    async def _reserve(self, state: RunState) -> None:
        self._check_budget(state)
        if self._store is not None:
            reserved = await self._store.reserve(state.context, self._max_calls)
            if not reserved:
                raise ToolFailure("call_budget", "Достигнут лимит вызовов исходного события.")

        state.calls_used += 1

    def _validate_batch(self, calls: Sequence[ToolCall]) -> list[ValidatedCall]:
        client = self._client()
        mutations = 0
        validated: list[ValidatedCall] = []
        identifiers: set[str] = set()

        for call in calls:
            if call.id in identifiers:
                raise ToolFailure(
                    "invalid_arguments", "Вызовы инструментов должны иметь разные ID."
                )
            identifiers.add(call.id)

            if call.name == READ_RESOURCE_FUNCTION:
                try:
                    arguments = ReadResourceArguments.model_validate(call.arguments)
                except ValidationError:
                    raise ToolFailure("invalid_arguments", "Уточните URI ресурса.") from None

                if arguments.uri not in client.resources:
                    raise ToolFailure("resource_unavailable", "Ресурс не обнаружен у MCP-сервера.")
                validated.append((call, arguments))
                continue

            client.validate_arguments(call.name, call.arguments)
            if call.name == REMINDER_TOOL_NAME:
                mutations += 1
            elif not client.is_read_only(call.name):
                raise ToolFailure(
                    "unsupported_action", "Для этого действия не настроено подтверждение."
                )
            validated.append((call, None))

        if mutations > 1:
            raise ToolFailure("multiple_mutations", "Подтверждайте напоминания по одному.")
        return validated

    async def execute(
        self,
        state: RunState,
        calls: Sequence[ToolCall],
    ) -> AgentResult | StepAction:
        validated = self._validate_batch(calls)

        for call, resource_arguments in validated:
            if resource_arguments is not None:
                await self._read_resource_call(state, call, resource_arguments.uri)
                continue

            state.last_call = call

            if call.name == REMINDER_TOOL_NAME:
                return await self._prepare(state, call)

            await self._read(state, call)

        return StepAction.CONTINUE

    async def _prepare(self, state: RunState, call: ToolCall) -> AgentResult:
        self._check_budget(state)
        if self._confirmations is None:
            raise ToolFailure("confirmation_unavailable", "Подтверждения сейчас недоступны.")

        result = await self._confirmations.prepare(
            state.context,
            call.arguments,
            state.timezone,
            position=0,
            calls_remaining=self._max_calls - state.calls_used,
        )
        return result.model_copy(
            update={
                "reason": "awaiting_confirmation",
                "resource_uris": list(state.resource_uris),
                "images": list(state.images),
            }
        )

    def _add_images(self, state: RunState, images: Sequence[ToolImage]) -> None:
        if not images:
            return

        if len(state.images) + len(images) > MAX_AGENT_IMAGES:
            raise ToolFailure(
                "tool_output_budget",
                "Инструмент вернул слишком много изображений. Уточните запрос.",
            )

        known_bytes = sum(
            len(base64.b64decode(image.data, validate=True)) for image in state.images
        )
        added_bytes = sum(len(base64.b64decode(image.data, validate=True)) for image in images)
        if known_bytes + added_bytes > MAX_AGENT_IMAGE_BYTES:
            raise ToolFailure(
                "tool_output_budget",
                "Изображения результата слишком большие. Уточните запрос.",
            )

        state.images.extend(images)

    @staticmethod
    def _raise_if_deadline_expired(state: RunState) -> None:
        if state.deadline_at is None:
            return
        if state.deadline_at <= asyncio.get_running_loop().time():
            raise ToolFailure("request_deadline", REQUEST_DEADLINE_MESSAGE)

    async def _deadline_bound(
        self,
        state: RunState,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Bound one tool stage by the request deadline without hiding component timeouts."""
        self._raise_if_deadline_expired(state)
        if state.deadline_at is None:
            return await operation()

        timeout = asyncio.timeout_at(state.deadline_at)
        try:
            async with timeout:
                return await operation()
        except TimeoutError:
            if timeout.expired():
                raise ToolFailure("request_deadline", REQUEST_DEADLINE_MESSAGE) from None
            raise

    async def _read(self, state: RunState, call: ToolCall) -> None:
        client = self._client()
        code = "ok"
        started = time.monotonic()

        async def reserve_and_call() -> dict[str, object] | PhotoToolResult:
            await self._reserve(state)
            return await client.call_tool(call.name, call.arguments)

        try:
            outcome = await self._deadline_bound(state, reserve_and_call)
        except ToolFailure as error:
            code = error.code
            raise
        finally:
            logger.info(
                "tool_complete request=%s tool=%s duration_ms=%.0f code=%s",
                state.context.request_id,
                call.name,
                (time.monotonic() - started) * 1000,
                code,
            )

        if isinstance(outcome, PhotoToolResult):
            payload = outcome.payload
            images = outcome.images
        else:
            payload = outcome
            images = []

        content = json.dumps({"source": call.name, "data": payload}, ensure_ascii=False)
        if len(content) > self._max_output_chars:
            raise ToolFailure(
                "tool_output_budget", "Результат инструмента слишком большой. Уточните запрос."
            )

        self._add_images(state, images)
        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=content,
            )
        )

    async def _read_resource_call(self, state: RunState, call: ToolCall, uri: str) -> None:
        client = self._client()
        code = "ok"
        started = time.monotonic()
        payload: dict[str, object] = {"source": "mcp_resource", "uri": uri}
        reserved = False

        async def reserve_and_read() -> ResourceContent:
            nonlocal reserved
            await self._reserve(state)
            reserved = True
            return await client.read_resource(uri)

        try:
            resource = await self._deadline_bound(state, reserve_and_read)
            if len(resource.text) > self._max_output_chars:
                raise ToolFailure("tool_output_budget", "Результат ресурса слишком большой.")
            payload.update(mime_type=resource.mime_type, content=resource.text)
            state.resource_uris.append(resource.uri)
        except TimeoutError:
            code = "timeout"
            raise
        except ToolFailure as error:
            code = error.code
            if not reserved or error.code == "request_deadline":
                raise
            payload["error"] = {"code": error.code, "message": error.message}
        finally:
            logger.info(
                "resource_complete request=%s duration_ms=%.0f code=%s",
                state.context.request_id,
                (time.monotonic() - started) * 1000,
                code,
            )

        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=json.dumps(payload, ensure_ascii=False),
            )
        )
