from dataclasses import dataclass

from app.core.schemas.agent import EventContext
from app.core.schemas.message import ChatMessage, ToolCall


@dataclass
class RunState:
    context: EventContext
    conversation: list[ChatMessage]
    timezone: str | None
    dry_run: bool = False
    calls_used: int = 0
    last_call: ToolCall | None = None
