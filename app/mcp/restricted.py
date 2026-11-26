import json
from collections.abc import Mapping, Set

from pydantic import ValidationError

from app.core.ports import ToolClient
from app.core.schemas.knowledge import (
    KnowledgeStatus,
    MaterialCatalog,
    MaterialSearchResult,
    WebSearchResult,
)
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.mcp import ResourceContent, ResourceDefinition
from app.core.schemas.specialist import KnowledgeFact, MathFact, ScheduleFact, WeatherFact
from app.core.schemas.tools import PhotoToolResult, ToolFailure
from app.mcp.request_context import current_mcp_request


class RestrictedToolClient:
    def __init__(
        self,
        client: ToolClient,
        tools: Set[str],
        resources: Set[str] = frozenset(),
        *,
        trusted_reminder: bool = False,
    ) -> None:
        self._client = client
        self._tool_names = frozenset(tools)
        self._resource_uris = frozenset(resources)
        self._trusted_reminder = trusted_reminder

    @property
    def available(self) -> bool:
        return self._client.available and bool(self.tools or self.resources)

    @property
    def tools(self) -> Mapping[str, ToolDefinition]:
        context = current_mcp_request()
        allowed = self._tool_names
        if context is None or not context.web_search_allowed:
            allowed = allowed - {"search_web"}
        if (context is None or not context.reminder_allowed) and not self._trusted_reminder:
            allowed = allowed - {"add_reminder"}
        return {name: tool for name, tool in self._client.tools.items() if name in allowed}

    @property
    def resources(self) -> Mapping[str, ResourceDefinition]:
        return {
            uri: resource
            for uri, resource in self._client.resources.items()
            if uri in self._resource_uris
        }

    def is_read_only(self, name: str) -> bool:
        self._require_tool(name)
        return self._client.is_read_only(name)

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        self._require_tool(name)
        self._client.validate_arguments(name, arguments)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object] | PhotoToolResult:
        self._require_tool(name)
        self._client.validate_arguments(name, arguments)
        context = current_mcp_request()
        if context is not None and user_id is not None and user_id != context.user_id:
            raise ToolFailure("unauthorized", "Несовпадение владельца запроса.")
        if not self._client.is_read_only(name) and (user_id is None or confirmation_id is None):
            raise ToolFailure("confirmation_required", "Нужно подтверждение владельца.")
        if context is not None:
            context.attempted_sources.add(name)
        result = await self._client.call_tool(
            name, arguments, user_id=user_id, confirmation_id=confirmation_id
        )
        if context is not None:
            payload = result.payload if isinstance(result, PhotoToolResult) else result
            context.facts.extend(_tool_facts(name, payload))
        return result

    async def read_resource(self, uri: str) -> ResourceContent:
        if uri not in self.resources:
            raise ToolFailure("resource_unavailable", "Ресурс недоступен специалисту.")
        result = await self._client.read_resource(uri)
        context = current_mcp_request()
        if uri == "schedule://current-week":
            try:
                payload: object = json.loads(result.text)
            except ValueError:
                raise ToolFailure(
                    "invalid_output", "Ресурс расписания вернул некорректный JSON."
                ) from None
            if not isinstance(payload, dict):
                raise ToolFailure("invalid_output", "Ресурс расписания вернул некорректный формат.")
            if context is not None:
                context.facts.append(ScheduleFact(source=uri, data=payload))
        elif uri == "materials://catalog":
            try:
                MaterialCatalog.model_validate_json(result.text)
            except ValidationError:
                raise ToolFailure(
                    "invalid_output", "Каталог материалов вернул некорректные данные."
                ) from None
        return result

    def _require_tool(self, name: str) -> None:
        if name not in self.tools:
            raise ToolFailure("unknown_tool", "Инструмент недоступен специалисту.")


def _tool_facts(
    name: str, payload: dict[str, object]
) -> list[KnowledgeFact | ScheduleFact | WeatherFact | MathFact]:
    match name:
        case "search_materials" | "search_web":
            schema = MaterialSearchResult if name == "search_materials" else WebSearchResult
            try:
                parsed = schema.model_validate(payload)
            except ValidationError:
                raise ToolFailure("invalid_output", "Поиск вернул некорректные данные.") from None
            if parsed.status is KnowledgeStatus.EMPTY and parsed.results:
                raise ToolFailure("invalid_output", "Поиск вернул противоречивый пустой статус.")
            return [KnowledgeFact(source=item) for item in parsed.results]
        case "get_schedule" | "get_schedule_week":
            return [ScheduleFact(source=name, data=payload)]
        case "get_weather":
            return [WeatherFact(data=payload)]
        case "math_calculate" | "math_solve" | "math_plot":
            return [MathFact(operation=name, data=payload)]
        case _:
            return []
