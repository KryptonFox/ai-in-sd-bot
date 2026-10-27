import logging
from dataclasses import dataclass

import asyncpg

from app.config import Settings
from app.core.budget import ContextBudget
from app.core.exceptions import LLMError
from app.core.schemas.llm import LLMRequest, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import ToolFailure
from app.db.repositories import history_repo
from app.llm.base import BaseLLMClient
from app.modes.base import BotMode
from app.prompts.compaction import compaction_messages, summary_message

logger = logging.getLogger("app.context")


@dataclass(frozen=True)
class PreparedContext:
    messages: list[ChatMessage]
    budget: ContextBudget


def complete_pairs(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    pairs: list[list[ChatMessage]] = []
    pending: ChatMessage | None = None
    for message in messages:
        if message.role == MessageRole.USER:
            pending = message
        elif message.role == MessageRole.ASSISTANT and pending is not None:
            pairs.append([pending, message])
            pending = None
    return pairs


class ContextBuilder:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, llm: BaseLLMClient) -> None:
        self._pool = pool
        self._settings = settings
        self._llm = llm

    async def budget(self) -> ContextBudget:
        try:
            window = await self._llm.get_context_length(self._settings.llm_model)
            if not isinstance(window, int) or window < 512:
                window = self._settings.default_context_length
        except (LLMError, TypeError, ValueError):
            window = self._settings.default_context_length
        return ContextBudget(window, min(self._settings.llm_max_tokens, window // 2))

    async def build(
        self,
        user_id: int,
        text: str,
        mode: BotMode,
        tools: list[ToolDefinition],
        instructions: str,
        action: ChatMessage | None = None,
    ) -> PreparedContext:
        budget = await self.budget()
        await self.compact(user_id, budget)
        summary = await history_repo.get_dialog_summary(self._pool, user_id)
        initial = mode.build_initial_messages(text)
        if summary:
            initial.append(summary_message(summary))
        if action:
            initial.append(action)
        current = ChatMessage(role=MessageRole.USER, content=text)
        history = await history_repo.get_dialog_history(
            self._pool,
            user_id,
            self._settings.history_max_messages,
            self._settings.history_max_chars,
        )
        pairs = complete_pairs(history)
        overhead = len(instructions.encode()) + 64
        while pairs:
            messages = initial + [item for pair in pairs for item in pair] + [current]
            if budget.fits(messages, tools, overhead):
                return PreparedContext(messages, budget)
            pairs.pop(0)
        if not budget.fits([*initial, current], tools, overhead):
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )
        return PreparedContext([*initial, current], budget)

    async def compact(self, user_id: int, budget: ContextBudget | None = None) -> bool:
        budget = budget or await self.budget()
        rows = await history_repo.get_user_messages_with_ids(self._pool, user_id)
        if len(rows) < self._settings.compaction_threshold_messages:
            return False
        keep = max(2, ((self._settings.compaction_keep_recent + 1) // 2) * 2)
        candidates = rows[:-keep]
        pairs = complete_pairs([message for _, message in candidates])
        if not pairs:
            return False
        old_summary = await history_repo.get_dialog_summary(self._pool, user_id)
        selected: list[ChatMessage] = []
        for pair in pairs:
            proposed = compaction_messages(old_summary, [*selected, *pair])
            if not budget.fits(proposed, []):
                break
            selected.extend(pair)
        if not selected:
            return False
        try:
            response = await self._llm.generate(
                LLMRequest(
                    messages=compaction_messages(old_summary, selected),
                    temperature=0.2,
                    model=self._settings.llm_model,
                    max_tokens=min(512, budget.output_tokens),
                )
            )
        except LLMError:
            logger.warning("compaction_unavailable")
            return False
        summary = response.content.strip()
        if (
            not summary
            or len(summary) > self._settings.summary_max_chars
            or response.tool_calls
            or response.finish_reason != "stop"
        ):
            return False
        selected_ids = [
            identifier
            for identifier, message in candidates
            if any(message is item for item in selected)
        ]
        await history_repo.commit_compaction(self._pool, user_id, selected_ids, summary)
        return True
