import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage
from app.core.schemas.tools import ToolFailure

MIN_GENERATION_TOKENS = 64


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output_tokens: int = 16384

    @staticmethod
    def estimate_message(message: ChatMessage) -> int:
        serialized_message = message.model_dump_json().encode()
        message_overhead = 16
        return len(serialized_message) + message_overhead

    def estimate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> int:
        message_size = sum(self.estimate_message(message) for message in messages)
        tool_schemas = [tool.model_dump() for tool in tools]
        serialized_tools = json.dumps(tool_schemas, ensure_ascii=False).encode()

        return message_size + len(serialized_tools)

    def fits(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
        extra: int = 0,
    ) -> bool:
        required_space = self.estimate(messages, tools) + self.output_tokens + extra
        return required_space <= self.window

    def generation_limit(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition]
    ) -> int:
        remaining = self.window - self.estimate(messages, tools)
        if remaining < MIN_GENERATION_TOKENS:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )

        return min(self.output_tokens, remaining)
