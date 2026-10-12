from typing import Protocol

from app.core.schemas.llm import LLMRequest, LLMResponse


class BaseLLMClient(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    async def get_context_length(self, model: str | None = None) -> int: ...
