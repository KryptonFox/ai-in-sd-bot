import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage
from app.core.schemas.tools import ToolFailure


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output_tokens: int = 16384

    def estimate(self, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition]) -> int:
        return sum(len(message.model_dump_json().encode()) + 16 for message in messages) + len(
            json.dumps([tool.model_dump() for tool in tools], ensure_ascii=False).encode()
        )

    def fits(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition], extra: int = 0
    ) -> bool:
        return self.estimate(messages, tools) + self.output_tokens + extra <= self.window

    def generation_limit(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition]
    ) -> int:
        remaining = self.window - self.estimate(messages, tools)
        if remaining < 64:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )
        return min(self.output_tokens, remaining)
