import json
import logging
import time
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from app.core.ports import ToolClient, TurnStore
from app.core.schemas.agent import AgentResult
from app.core.schemas.execution import RunState, StepAction
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.mcp import ReadResourceArguments, ResourceDefinition
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import ToolFailure
from app.services.confirmation_service import ConfirmationService

logger = logging.getLogger("app.tools")
REMINDER_TOOL_NAME = "add_reminder"
READ_RESOURCE_FUNCTION = "read_resource"

type ValidatedCall = tuple[ToolCall, ReadResourceArguments | None]


class ToolExecutionService:
    def __init__(
        self,
        mcp: ToolClient | None = None,
        confirmations: ConfirmationService | None = None,
        store: TurnStore | None = None,
        *,
        max_calls: int = 2,
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
            }
        )

    async def _read(self, state: RunState, call: ToolCall) -> None:
        client = self._client()
        await self._reserve(state)
        started = time.monotonic()
        payload = await client.call_tool(call.name, call.arguments)

        logger.info(
            "tool_complete request=%s tool=%s duration_ms=%.0f",
            state.context.request_id,
            call.name,
            (time.monotonic() - started) * 1000,
        )
        content = json.dumps({"source": call.name, "data": payload}, ensure_ascii=False)
        if len(content) > self._max_output_chars:
            raise ToolFailure(
                "tool_output_budget", "Результат инструмента слишком большой. Уточните запрос."
            )

        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=content,
            )
        )

    async def _read_resource_call(self, state: RunState, call: ToolCall, uri: str) -> None:
        client = self._client()
        await self._reserve(state)
        started = time.monotonic()
        payload: dict[str, object] = {"source": "mcp_resource", "uri": uri}
        try:
            resource = await client.read_resource(uri)
            if len(resource.text) > self._max_output_chars:
                raise ToolFailure("tool_output_budget", "Результат ресурса слишком большой.")
            payload.update(mime_type=resource.mime_type, content=resource.text)
            state.resource_uris.append(resource.uri)
        except ToolFailure as error:
            payload["error"] = {"code": error.code, "message": error.message}

        logger.info(
            "resource_complete request=%s duration_ms=%.0f",
            state.context.request_id,
            (time.monotonic() - started) * 1000,
        )
        state.conversation.append(
            ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=call.id,
                content=json.dumps(payload, ensure_ascii=False),
            )
        )
