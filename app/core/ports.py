from collections.abc import Mapping
from typing import Protocol

from app.core.schemas.llm import ToolDefinition


class ToolExecutor(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        user_id: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]: ...


class ToolClient(ToolExecutor, Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def tools(self) -> Mapping[str, ToolDefinition]: ...

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None: ...
