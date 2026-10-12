from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.message import ChatMessage


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[ChatMessage]
    temperature: float = 0.7
    model: str | None = None
    max_tokens: int | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    finish_reason: str = "stop"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
