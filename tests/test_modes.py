from app.core.schemas.message import MessageRole
from app.modes.base import BotMode, FewShotExample
from app.modes.definitions import (
    REVIEW_MODE,
    STUDY_MODE,
    TELEGRAM_FORMATTING_PROMPT,
    TRANSLATE_MODE,
)
from app.modes.registry import ModeRegistry


def test_mode_registry_default():
    reg = ModeRegistry(default_id="study")
    reg.register(STUDY_MODE)
    reg.register(TRANSLATE_MODE)
    reg.register(REVIEW_MODE)

    assert reg.get_or_default("unknown").id == "study"
    translate = reg.get("translate")
    review = reg.get("review")
    assert translate is not None
    assert review is not None
    assert translate.name == "Переводчик"
    assert review.command == "review"
    assert "study" in reg.commands()
    assert "translate" in reg.commands()
    assert "review" in reg.commands()


def test_custom_mode_registration():
    reg = ModeRegistry()
    custom = BotMode(
        id="custom",
        command="custom",
        name="Пользовательский",
        description="Тестовый режим",
        system_prompt="Role: Test Assistant",
    )

    reg.register(custom)

    assert reg.get("custom") == custom
    assert "custom" in reg.commands()
    assert "/custom" in reg.format_help()


def test_few_shot_building():
    mode = BotMode(
        id="test",
        command="test",
        name="Test",
        description="Test",
        system_prompt="System Prompt",
        few_shot_examples=[FewShotExample(user_input="Hi", assistant_output="Hello!")],
    )

    messages = mode.build_initial_messages()

    assert len(messages) == 3
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[0].content == "System Prompt"
    assert messages[1].role == MessageRole.USER
    assert messages[1].content == "Hi"
    assert messages[2].role == MessageRole.ASSISTANT
    assert messages[2].content == "Hello!"


def test_telegram_formatting_prompt_included():
    for mode in (STUDY_MODE, TRANSLATE_MODE, REVIEW_MODE):
        assert TELEGRAM_FORMATTING_PROMPT in mode.system_prompt
        assert "Telegram Formatting Guidelines" in mode.system_prompt


def test_rctf_and_shared_rules_once():
    from app.prompts.compaction import COMPACTION_PROMPT
    from app.prompts.policy import SAFETY_TASK, TRUST_CONTEXT

    for prompt in [m.system_prompt for m in (STUDY_MODE, TRANSLATE_MODE, REVIEW_MODE)] + [
        COMPACTION_PROMPT
    ]:
        positions = []
        for heading in ("Role:", "Context:", "Task:", "Format:"):
            assert prompt.splitlines().count(heading) == 1
            positions.append(prompt.index(heading))
        assert positions == sorted(positions)
        assert prompt.count(SAFETY_TASK) == 1
        assert prompt.count(TRUST_CONTEXT) == 1
    assert TELEGRAM_FORMATTING_PROMPT not in COMPACTION_PROMPT


def test_rctf_requires_nonempty_fields():
    import pytest
    from pydantic import ValidationError

    from app.prompts.builder import PromptParts

    with pytest.raises(ValidationError):
        PromptParts(role="role", context="   ", task="task", format="format")


def test_relevant_examples_are_bounded_and_keep_languages():
    for query, marker in [
        ("C++ vector", "cpp"),
        ("C# string", "csharp"),
        ("Java ArrayList", "java"),
        ("Python duplicates", "python"),
    ]:
        messages = REVIEW_MODE.build_initial_messages(query)
        assert len(messages) == 3
        assert "```" + marker in messages[-1].content
        assert sum(len(m.content) for m in messages[1:]) <= 1800
    math = STUDY_MODE.build_initial_messages("Объясни индукцию")
    assert "```" not in math[-1].content
    assert len(STUDY_MODE.build_initial_messages("Привет")) == 1
