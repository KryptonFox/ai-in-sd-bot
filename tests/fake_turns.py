from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.schemas.agent import (
    Acquired,
    AgentDecision,
    AgentResult,
    EventContext,
    InProgress,
    Replay,
    TurnClaim,
    TurnState,
)
from app.core.schemas.message import ChatMessage
from app.db.repositories.turn_repo import INTERRUPTED_TEXT


class MemoryHistory(Protocol):
    messages: list[dict[str, object]]
    _msg_id_counter: int


@dataclass
class MemoryTurn:
    calls: int = 0
    state: TurnState = TurnState.PROCESSING
    result: AgentResult | None = None


class MemoryTurnStore:
    def __init__(self, history: MemoryHistory) -> None:
        self.history = history
        self.turns: dict[tuple[int, str], MemoryTurn] = {}

    async def claim(self, context: EventContext, now: datetime) -> TurnClaim:
        key = context.user_id, context.event_id
        turn = self.turns.get(key)
        if turn is None:
            self.turns[key] = MemoryTurn()
            return Acquired()
        if turn.result is not None:
            return Replay(turn.result)
        if turn.state == TurnState.FAILED:
            turn.state = TurnState.PROCESSING
            return Acquired(turn.calls)
        return InProgress()

    async def reserve(self, context: EventContext, limit: int) -> bool:
        turn = self.turns[context.user_id, context.event_id]
        if turn.state != TurnState.PROCESSING or turn.calls >= limit:
            return False
        turn.calls += 1
        return True

    async def complete(
        self,
        context: EventContext,
        message: ChatMessage,
        assistant: ChatMessage,
        result: AgentResult,
        now: datetime,
        duration_ms: float,
    ) -> None:
        turn = self.turns[context.user_id, context.event_id]
        assert turn.state == TurnState.PROCESSING
        for item in (message, assistant):
            self.history.messages.append(
                {
                    "id": self.history._msg_id_counter,
                    "user_id": context.user_id,
                    "role": item.role.value,
                    "content": item.content,
                    "char_count": item.char_count,
                    "metadata": {},
                    "event_id": context.event_id,
                }
            )
            self.history._msg_id_counter += 1
        turn.result = result
        turn.state = TurnState.DONE

    async def interrupt(self, context: EventContext, reason: str, now: datetime) -> bool:
        turn = self.turns[context.user_id, context.event_id]
        turn.state = TurnState.INTERRUPTED if turn.calls else TurnState.FAILED
        if turn.calls:
            turn.result = AgentResult(
                text=INTERRUPTED_TEXT,
                decision=AgentDecision.ERROR,
                reason="interrupted",
            )
        return bool(turn.calls)

    async def recover(self, now: datetime) -> None:
        for turn in self.turns.values():
            if turn.state == TurnState.PROCESSING:
                turn.state = TurnState.INTERRUPTED
                turn.result = AgentResult(
                    text=INTERRUPTED_TEXT,
                    decision=AgentDecision.ERROR,
                    reason="interrupted",
                )
