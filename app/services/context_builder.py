import logging
from dataclasses import dataclass

import asyncpg

from app.config import Settings
from app.core.budget import MIN_GENERATION_TOKENS, ContextBudget
from app.core.exceptions import LLMError
from app.core.schemas.llm import FinishReason, LLMRequest, ToolDefinition
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
    def __init__(
        self,
        pool: asyncpg.Pool,
        settings: Settings,
        llm: BaseLLMClient,
    ) -> None:
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
        return self._fit_context(
            initial,
            current,
            pairs,
            tools,
            overhead,
            budget,
        )

    @staticmethod
    def _fit_context(
        initial: list[ChatMessage],
        current: ChatMessage,
        pairs: list[list[ChatMessage]],
        tools: list[ToolDefinition],
        overhead: int,
        budget: ContextBudget,
    ) -> PreparedContext:
        fixed_size = budget.estimate([*initial, current], tools) + overhead
        available_context = budget.window - fixed_size
        if min(budget.output_tokens, available_context) < MIN_GENERATION_TOKENS:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )

        history_limit = available_context - MIN_GENERATION_TOKENS
        pair_sizes: list[int] = []

        for user_message, assistant_message in pairs:
            pair_size = budget.estimate_message(user_message)
            pair_size += budget.estimate_message(assistant_message)
            pair_sizes.append(pair_size)

        history_size = sum(pair_sizes)
        first_pair = 0

        while history_size > history_limit:
            history_size -= pair_sizes[first_pair]
            first_pair += 1

        output_tokens = min(budget.output_tokens, available_context - history_size)
        fitted_budget = ContextBudget(budget.window, output_tokens)
        messages = list(initial)

        for pair in pairs[first_pair:]:
            messages.extend(pair)

        messages.append(current)

        return PreparedContext(messages, fitted_budget)

    async def compact(
        self,
        user_id: int,
        budget: ContextBudget | None = None,
    ) -> bool:
        budget = budget or await self.budget()
        rows = await history_repo.get_user_messages_with_ids(self._pool, user_id)
        if len(rows) < self._settings.compaction_threshold_messages:
            return False

        keep = max(2, self._settings.compaction_keep_recent)
        if keep % 2:
            keep += 1

        old_summary = await history_repo.get_dialog_summary(self._pool, user_id)
        selected_ids, selected = self._select_compaction_input(
            rows[:-keep],
            old_summary,
            budget,
        )
        if not selected:
            return False

        summary = await self._summarize(old_summary, selected, budget)
        if summary is None:
            return False

        await history_repo.commit_compaction(
            self._pool,
            user_id,
            selected_ids,
            summary,
        )

        return True

    @staticmethod
    def _select_compaction_input(
        rows: list[tuple[int, ChatMessage]], summary: str | None, budget: ContextBudget
    ) -> tuple[list[int], list[ChatMessage]]:
        """Select complete dialog pairs while retaining their original record IDs."""
        selected_ids: list[int] = []
        selected_messages: list[ChatMessage] = []
        pending: tuple[int, ChatMessage] | None = None

        for identifier, message in rows:
            if message.role == MessageRole.USER:
                pending = identifier, message
                continue

            if message.role != MessageRole.ASSISTANT or pending is None:
                continue

            user_message_id, user_message = pending
            proposed = compaction_messages(summary, [*selected_messages, user_message, message])
            if not budget.fits(proposed, []):
                break

            selected_ids.extend((user_message_id, identifier))
            selected_messages.extend((user_message, message))
            pending = None

        return selected_ids, selected_messages

    async def _summarize(
        self,
        old_summary: str | None,
        selected: list[ChatMessage],
        budget: ContextBudget,
    ) -> str | None:
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
            return None

        summary = response.content.strip()
        if (
            not summary
            or len(summary) > self._settings.summary_max_chars
            or response.tool_calls
            or response.finish_reason != FinishReason.STOP
        ):
            return None

        return summary
