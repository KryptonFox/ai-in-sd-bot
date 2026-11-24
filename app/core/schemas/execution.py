from dataclasses import dataclass, field
from enum import StrEnum

from app.core.schemas.agent import AgentDecision, AgentResult, EventContext
from app.core.schemas.message import ChatMessage, ToolCall
from app.core.schemas.tools import ToolImage


class StepAction(StrEnum):
    CONTINUE = "continue"


@dataclass
class RunState:
    context: EventContext
    conversation: list[ChatMessage]
    timezone: str | None
    calls_used: int = 0
    last_call: ToolCall | None = None
    resource_uris: list[str] = field(default_factory=list)
    images: list[ToolImage] = field(default_factory=list)
    protected_prefix: int = 0
    deadline_at: float | None = None

    def build_result(
        self,
        text: str,
        decision: AgentDecision,
        reason: str,
        *,
        truncated: bool = False,
    ) -> AgentResult:
        tool_name = None
        arguments: dict[str, object] = {}
        if self.last_call is not None:
            tool_name = self.last_call.name
            arguments = self.last_call.arguments

        return AgentResult(
            text=text,
            decision=decision,
            reason=reason,
            tool_name=tool_name,
            arguments=arguments,
            resource_uris=list(self.resource_uris),
            images=list(self.images),
            truncated=truncated,
        )
