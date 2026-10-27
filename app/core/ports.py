from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from app.core.schemas.agent import AgentResult, EventContext, TurnClaim
from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage


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


class TurnStore(Protocol):
    async def claim(self, context: EventContext, now: datetime) -> TurnClaim: ...

    async def reserve(self, context: EventContext, limit: int) -> bool: ...

    async def complete(
        self,
        context: EventContext,
        message: ChatMessage,
        assistant: ChatMessage,
        result: AgentResult,
        now: datetime,
        duration_ms: float,
    ) -> None: ...

    async def interrupt(self, context: EventContext, reason: str, now: datetime) -> bool: ...

    async def recover(self, now: datetime) -> None: ...
