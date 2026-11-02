from dataclasses import dataclass

from app.core.schemas.agent import AgentDecision, AgentResult, EventContext
from app.core.schemas.message import ChatMessage, ToolCall


@dataclass
class RunState:
    context: EventContext
    conversation: list[ChatMessage]
    timezone: str | None
    dry_run: bool = False
    calls_used: int = 0
    last_call: ToolCall | None = None

    def build_result(
        self,
        text: str,
        decision: AgentDecision,
        reason: str,
        *,
        planned: bool = False,
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
            planned=planned,
            truncated=truncated,
        )
