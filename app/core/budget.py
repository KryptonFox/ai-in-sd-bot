import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.message import ChatMessage
from app.core.schemas.tools import ToolFailure

MIN_GENERATION_TOKENS = 64
DEFAULT_GENERATION_RESERVE_TOKENS = 4096
GENERATION_RESERVE_WINDOW_DIVISOR = 8


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output_tokens: int = 16384
    generation_reserve: int = DEFAULT_GENERATION_RESERVE_TOKENS

    @property
    def effective_generation_reserve(self) -> int:
        window_reserve = max(
            MIN_GENERATION_TOKENS,
            self.window // GENERATION_RESERVE_WINDOW_DIVISOR,
        )
        return min(self.generation_reserve, self.output_tokens, window_reserve)

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

    def remaining_space(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> int:
        return self.window - self.estimate(messages, tools)

    def has_generation_reserve(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> bool:
        return self.remaining_space(messages, tools) >= max(
            self.effective_generation_reserve,
            MIN_GENERATION_TOKENS,
        )

    def generation_limit(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition]
    ) -> int:
        remaining = self.remaining_space(messages, tools)
        required_reserve = max(self.effective_generation_reserve, MIN_GENERATION_TOKENS)
        if remaining < required_reserve:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )

        return min(self.output_tokens, remaining)
