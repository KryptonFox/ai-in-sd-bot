from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.message import ChatMessage, ToolCall


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[ChatMessage]
    temperature: float = 0.7
    model: str | None = None
    max_tokens: int | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    finish_reason: str = "stop"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
