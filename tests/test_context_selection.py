from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from app.config import Settings
from app.core.budget import ContextBudget
from app.core.schemas.llm import LLMResponse, ToolDefinition
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import ToolFailure
from app.db.repositories import history_repo
from app.modes.base import BotMode
from app.prompts.compaction import compaction_messages, summary_message
from app.services.context_builder import ContextBuilder, complete_pairs

MODE = BotMode(
    id="test",
    command="test",
    name="test",
    description="test",
    system_prompt="Правила",
)


@pytest.fixture
def llm() -> MagicMock:
    return MagicMock(generate=AsyncMock(return_value=LLMResponse(content="Новая выжимка")))


@pytest.fixture
def builder(monkeypatch: pytest.MonkeyPatch, llm: MagicMock) -> ContextBuilder:
    monkeypatch.setattr(
        history_repo,
        "get_user_messages_with_ids",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        history_repo,
        "get_dialog_history",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        history_repo,
        "get_dialog_summary",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(history_repo, "commit_compaction", AsyncMock())
    settings = Settings(
        bot_token="unused",
        postgres_password="unused",
        compaction_threshold_messages=4,
        compaction_keep_recent=2,
    )

    return ContextBuilder(MagicMock(spec=asyncpg.Pool), settings, llm)


def test_complete_pairs_skip_orphans_and_keep_latest_pending_user() -> None:
    orphan = ChatMessage(role=MessageRole.ASSISTANT, content="Без вопроса")
    replaced = ChatMessage(role=MessageRole.USER, content="Первый вопрос")
    user = ChatMessage(role=MessageRole.USER, content="Второй вопрос")
    tool = ChatMessage(
        role=MessageRole.TOOL,
        content="Данные",
        tool_call_id="call",
    )
    assistant = ChatMessage(role=MessageRole.ASSISTANT, content="Ответ")
    trailing = ChatMessage(role=MessageRole.USER, content="Без ответа")
    assert complete_pairs([orphan, replaced, user, tool, assistant, trailing]) == [
        [user, assistant]
    ]


@pytest.mark.parametrize("available_extra", [0, -1, -64, -65])
async def test_context_keeps_only_complete_recent_pairs_at_budget_boundary(
    builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, available_extra: int
) -> None:
    history = [
        ChatMessage(role=MessageRole.USER, content="Старый вопрос " * 100),
        ChatMessage(role=MessageRole.ASSISTANT, content="Старый ответ " * 100),
        ChatMessage(role=MessageRole.USER, content="Новый вопрос " * 100),
        ChatMessage(role=MessageRole.ASSISTANT, content="Новый ответ " * 100),
    ]
    action = ChatMessage(role=MessageRole.USER, content="Проверенное действие")
    initial = [*MODE.build_initial_messages(), summary_message("Память"), action]
    current = ChatMessage(role=MessageRole.USER, content="Продолжим")
    tools = [
        ToolDefinition(
            name="погода",
            description="Описание",
            parameters={"type": "object"},
        )
    ]
    overhead = len("Инструкции".encode()) + 64
    estimate = ContextBudget(0, 128).estimate([*initial, *history[2:], current], tools)
    window = estimate + overhead + 128 + available_extra
    budget = ContextBudget(window, 128)
    monkeypatch.setattr(builder, "budget", AsyncMock(return_value=budget))
    monkeypatch.setattr(
        history_repo,
        "get_dialog_history",
        AsyncMock(return_value=history),
    )
    monkeypatch.setattr(
        history_repo,
        "get_dialog_summary",
        AsyncMock(return_value="Память"),
    )

    result = await builder.build(
        1,
        current.content,
        MODE,
        tools,
        "Инструкции",
        action,
    )

    expected_history = history[2:] if available_extra >= -64 else []
    assert result.messages == [*initial, *expected_history, current]
    assert len(history) == 4
    assert result.budget.fits(result.messages, tools, overhead)
    expected_output = 128 + available_extra if expected_history else 128
    assert result.budget.output_tokens == expected_output


async def test_context_rejects_base_prompt_that_cannot_fit(
    builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, llm: MagicMock
) -> None:
    monkeypatch.setattr(
        builder,
        "budget",
        AsyncMock(return_value=ContextBudget(64, 32)),
    )

    with pytest.raises(ToolFailure) as failure:
        await builder.build(1, "Вопрос", MODE, [], "Инструкции")

    assert failure.value.code == "context_budget"
    llm.generate.assert_not_awaited()


@pytest.mark.parametrize("remaining_for_answer", [64, 1024])
async def test_recent_history_takes_priority_over_maximum_output_reserve(
    builder: ContextBuilder,
    monkeypatch: pytest.MonkeyPatch,
    remaining_for_answer: int,
) -> None:
    history = [
        ChatMessage(role=MessageRole.USER, content="Код проекта: Альфа"),
        ChatMessage(role=MessageRole.ASSISTANT, content="Запомнил код проекта."),
    ]
    current = ChatMessage(role=MessageRole.USER, content="Какой код проекта?")
    initial = MODE.build_initial_messages(current.content)
    instructions = "Инструкции"
    overhead = len(instructions.encode()) + 64
    input_size = ContextBudget(0).estimate([*initial, *history, current], []) + overhead
    budget = ContextBudget(input_size + remaining_for_answer, 16384)
    monkeypatch.setattr(builder, "budget", AsyncMock(return_value=budget))
    monkeypatch.setattr(history_repo, "get_dialog_history", AsyncMock(return_value=history))

    prepared = await builder.build(1, current.content, MODE, [], instructions)

    assert prepared.messages == [*initial, *history, current]
    assert prepared.budget.output_tokens == remaining_for_answer
    assert prepared.budget.fits(prepared.messages, [], overhead)
    assert budget.output_tokens == 16384


@pytest.mark.parametrize("available_output", [64, 127, 128])
async def test_context_reduces_output_reserve_when_base_prompt_fits(
    builder: ContextBuilder,
    monkeypatch: pytest.MonkeyPatch,
    available_output: int,
) -> None:
    current = ChatMessage(role=MessageRole.USER, content="Вопрос")
    initial = MODE.build_initial_messages(current.content)
    instructions = "Инструкции"
    fixed_size = ContextBudget(0).estimate([*initial, current], [])
    fixed_size += len(instructions.encode()) + 64
    budget = ContextBudget(fixed_size + available_output, 128)
    monkeypatch.setattr(builder, "budget", AsyncMock(return_value=budget))

    prepared = await builder.build(1, current.content, MODE, [], instructions)

    assert prepared.messages == [*initial, current]
    assert prepared.budget.output_tokens == available_output
    assert prepared.budget.fits(prepared.messages, [], len(instructions.encode()) + 64)
    assert budget.output_tokens == 128


async def test_context_rejects_prompt_without_minimum_generation_space(
    builder: ContextBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = ChatMessage(role=MessageRole.USER, content="Вопрос")
    initial = MODE.build_initial_messages(current.content)
    fixed_size = ContextBudget(0).estimate([*initial, current], [])
    fixed_size += len("Инструкции".encode()) + 64
    monkeypatch.setattr(
        builder,
        "budget",
        AsyncMock(return_value=ContextBudget(fixed_size + 63, 128)),
    )

    with pytest.raises(ToolFailure) as failure:
        await builder.build(1, current.content, MODE, [], "Инструкции")

    assert failure.value.code == "context_budget"


async def test_compaction_preserves_ids_when_records_share_a_message_object(
    builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, llm: MagicMock
) -> None:
    user = ChatMessage(role=MessageRole.USER, content="Одинаковый вопрос")
    assistant = ChatMessage(role=MessageRole.ASSISTANT, content="Ответ")
    rows = [(10, user), (11, user), (12, assistant), (13, assistant), (14, user), (15, assistant)]
    monkeypatch.setattr(
        history_repo,
        "get_user_messages_with_ids",
        AsyncMock(return_value=rows),
    )
    commit = AsyncMock()
    monkeypatch.setattr(history_repo, "commit_compaction", commit)

    assert await builder.compact(1, ContextBudget(32768, 512))

    assert commit.await_args is not None
    assert commit.await_args.args[1:] == (1, [11, 12], "Новая выжимка")
    assert llm.generate.await_args is not None
    request = llm.generate.await_args.args[0]
    assert request.messages == compaction_messages(None, [user, assistant])


@pytest.mark.parametrize(
    ("keep_recent", "summarized_ids"),
    [(3, [1, 2, 3, 4, 5, 6]), (4, [1, 2, 3, 4, 5, 6]), (5, [1, 2, 3, 4])],
)
async def test_compaction_rounds_recent_message_reserve_to_complete_pairs(
    builder: ContextBuilder,
    monkeypatch: pytest.MonkeyPatch,
    keep_recent: int,
    summarized_ids: list[int],
) -> None:
    builder._settings = replace(builder._settings, compaction_keep_recent=keep_recent)
    rows: list[tuple[int, ChatMessage]] = []

    for identifier in range(1, 11):
        role = MessageRole.USER if identifier % 2 else MessageRole.ASSISTANT
        rows.append((identifier, ChatMessage(role=role, content=str(identifier))))

    monkeypatch.setattr(
        history_repo,
        "get_user_messages_with_ids",
        AsyncMock(return_value=rows),
    )
    commit = AsyncMock()
    monkeypatch.setattr(history_repo, "commit_compaction", commit)

    assert await builder.compact(1, ContextBudget(32768, 512))

    assert commit.await_args is not None
    assert commit.await_args.args[2] == summarized_ids


async def test_compaction_stops_before_a_pair_that_does_not_fit(
    builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, llm: MagicMock
) -> None:
    first_user = ChatMessage(role=MessageRole.USER, content="Первый вопрос")
    first_answer = ChatMessage(role=MessageRole.ASSISTANT, content="Первый ответ")
    second_user = ChatMessage(role=MessageRole.USER, content="Большой вопрос " * 100)
    second_answer = ChatMessage(role=MessageRole.ASSISTANT, content="Большой ответ " * 100)
    rows = [
        (1, first_user),
        (2, first_answer),
        (3, second_user),
        (4, second_answer),
        (5, first_user),
        (6, first_answer),
    ]
    expected_request = compaction_messages("Предыдущая память", [first_user, first_answer])
    window = ContextBudget(0, 64).estimate(expected_request, []) + 64
    monkeypatch.setattr(
        history_repo,
        "get_user_messages_with_ids",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr(
        history_repo,
        "get_dialog_summary",
        AsyncMock(return_value="Предыдущая память"),
    )
    commit = AsyncMock()
    monkeypatch.setattr(history_repo, "commit_compaction", commit)

    assert await builder.compact(1, ContextBudget(window, 64))

    assert commit.await_args is not None
    assert commit.await_args.args[2] == [1, 2]
    assert llm.generate.await_args is not None
    assert llm.generate.await_args.args[0].messages == expected_request
