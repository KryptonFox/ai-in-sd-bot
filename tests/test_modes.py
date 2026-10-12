from app.core.schemas.message import MessageRole
from app.modes.base import BotMode, FewShotExample
from app.modes.definitions import (
    REVIEW_MODE,
    STUDY_MODE,
    TELEGRAM_FORMATTING_PROMPT,
    TRANSLATE_MODE,
    compose_prompt,
)
from app.modes.registry import ModeRegistry


def test_mode_registry_default():
    reg = ModeRegistry(default_id="study")
    reg.register(STUDY_MODE)
    reg.register(TRANSLATE_MODE)
    reg.register(REVIEW_MODE)

    assert reg.get_or_default("unknown").id == "study"
    assert reg.get("translate").name == "Переводчик"
    assert reg.get("review").command == "review"
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


def test_compose_prompt_custom():
    composed = compose_prompt("Custom Role")
    assert composed.startswith("Custom Role\n\n")
    assert TELEGRAM_FORMATTING_PROMPT in composed
