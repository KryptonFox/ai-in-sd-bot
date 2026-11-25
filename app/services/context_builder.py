import logging
from dataclasses import dataclass

import asyncpg

from app.config import Settings
from app.core.budget import (
    GENERATION_RESERVE_WINDOW_DIVISOR,
    MIN_GENERATION_TOKENS,
    ContextBudget,
)
from app.core.conversation import complete_pairs, complete_unit_indices
from app.core.exceptions import LLMError
from app.core.schemas.llm import FinishReason, LLMRequest, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import ToolFailure
from app.db.repositories import history_repo
from app.llm.base import BaseLLMClient
from app.modes.base import BotMode
from app.prompts.compaction import compaction_messages, summary_message

logger = logging.getLogger("app.context")
COMPACTION_MAX_TOKENS = 512


def _compaction_token_limit(budget: ContextBudget) -> int:
    window_limit = max(
        MIN_GENERATION_TOKENS,
        budget.window // GENERATION_RESERVE_WINDOW_DIVISOR,
    )
    return min(COMPACTION_MAX_TOKENS, budget.output_tokens, window_limit)


@dataclass(frozen=True)
class PreparedContext:
    messages: list[ChatMessage]
    budget: ContextBudget
    protected_prefix: int = 0


def _complete_indexed_units(
    rows: list[tuple[int, ChatMessage]],
) -> list[list[tuple[int, ChatMessage]]]:
    messages = [message for _, message in rows]
    return [[rows[index] for index in unit] for unit in complete_unit_indices(messages)]


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

        return ContextBudget(
            window,
            min(self._settings.llm_max_tokens, window // 2),
            self._settings.llm_generation_reserve_tokens,
        )

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
        mode_messages = mode.build_initial_messages(text)
        current = ChatMessage(role=MessageRole.USER, content=text)
        overhead = len(instructions.encode()) + 64
        pairs, summary = await self._load_history(user_id)
        initial = self._compose_initial(mode_messages, summary, action)

        needs_budget_compaction = self._needs_compaction(
            initial,
            current,
            pairs,
            tools,
            overhead,
            budget,
        )
        if await self.compact(user_id, budget, force=needs_budget_compaction):
            pairs, summary = await self._load_history(user_id)
            initial = self._compose_initial(mode_messages, summary, action)

        return self._fit_context(
            initial,
            current,
            pairs,
            tools,
            overhead,
            budget,
        )

    async def _load_history(
        self,
        user_id: int,
    ) -> tuple[list[list[ChatMessage]], str | None]:
        history = await history_repo.get_dialog_history(
            self._pool,
            user_id,
            self._settings.history_max_messages,
            self._settings.history_max_chars,
        )
        summary = await history_repo.get_dialog_summary(self._pool, user_id)
        return complete_pairs(history), summary

    @staticmethod
    def _compose_initial(
        mode_messages: list[ChatMessage],
        summary: str | None,
        action: ChatMessage | None,
    ) -> list[ChatMessage]:
        initial = list(mode_messages)
        if summary:
            initial.append(summary_message(summary))
        if action:
            initial.append(action)
        return initial

    @staticmethod
    def _needs_compaction(
        initial: list[ChatMessage],
        current: ChatMessage,
        pairs: list[list[ChatMessage]],
        tools: list[ToolDefinition],
        overhead: int,
        budget: ContextBudget,
    ) -> bool:
        required_reserve = max(budget.effective_generation_reserve, MIN_GENERATION_TOKENS)
        available = budget.window - budget.estimate([*initial, current], tools) - overhead
        history_size = sum(
            sum(budget.estimate_message(message) for message in pair) for pair in pairs
        )

        return history_size > available - required_reserve

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
        if available_context < MIN_GENERATION_TOKENS:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )

        generation_reserve = max(budget.effective_generation_reserve, MIN_GENERATION_TOKENS)
        history_limit = available_context - generation_reserve
        pair_sizes = [sum(budget.estimate_message(message) for message in pair) for pair in pairs]

        history_size = sum(pair_sizes)
        first_pair = 0

        while history_size > history_limit and first_pair < len(pairs):
            history_size -= pair_sizes[first_pair]
            first_pair += 1

        output_tokens = min(budget.output_tokens, available_context - history_size)
        if output_tokens < generation_reserve:
            raise ToolFailure(
                "context_budget",
                "Запрос или контекст слишком большой. Сократите запрос или очистите историю.",
            )

        fitted_budget = ContextBudget(
            budget.window,
            output_tokens,
            budget.generation_reserve,
        )
        messages = list(initial)

        for pair in pairs[first_pair:]:
            messages.extend(pair)

        messages.append(current)

        logger.info(
            "context_fit window=%d fixed=%d dropped_units=%d history=%d output=%d reserve=%d",
            budget.window,
            fixed_size,
            first_pair,
            history_size,
            output_tokens,
            generation_reserve,
        )

        return PreparedContext(messages, fitted_budget, len(initial))

    async def compact(
        self,
        user_id: int,
        budget: ContextBudget | None = None,
        *,
        force: bool = False,
    ) -> bool:
        budget = budget or await self.budget()
        rows = await history_repo.get_user_messages_with_ids(self._pool, user_id)
        if not force and len(rows) < self._settings.compaction_threshold_messages:
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
            logger.info(
                "context_compaction rows=%d selected_messages=%d result=skipped",
                len(rows),
                len(selected),
            )
            return False

        await history_repo.commit_compaction(
            self._pool,
            user_id,
            selected_ids,
            summary,
        )
        logger.info(
            "context_compaction rows=%d selected_messages=%d summary_chars=%d result=committed",
            len(rows),
            len(selected),
            len(summary),
        )

        return True

    @staticmethod
    def _select_compaction_input(
        rows: list[tuple[int, ChatMessage]], summary: str | None, budget: ContextBudget
    ) -> tuple[list[int], list[ChatMessage]]:
        """Select complete dialog units while retaining their original record IDs."""
        selected_ids: list[int] = []
        selected_messages: list[ChatMessage] = []
        summary_budget = ContextBudget(
            budget.window,
            _compaction_token_limit(budget),
            0,
        )

        for unit in _complete_indexed_units(rows):
            unit_messages = [message for _, message in unit]
            proposed = compaction_messages(summary, [*selected_messages, *unit_messages])
            if not summary_budget.fits(proposed, []):
                break

            selected_ids.extend(identifier for identifier, _ in unit)
            selected_messages.extend(unit_messages)

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
                    max_tokens=_compaction_token_limit(budget),
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
