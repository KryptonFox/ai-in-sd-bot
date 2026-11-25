from collections.abc import Sequence

from app.core.schemas.llm import LLMRequest, LLMResponse


class ScriptedLLM:
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(update={"messages": list(request.messages)}))
        return next(self._responses)

    async def get_context_length(self, model: str | None = None) -> int:
        return 32768

    async def aclose(self) -> None:
        pass
