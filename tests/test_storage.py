import pytest

from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.user import UserSettings


def test_user_settings_temperature_validation():
    for t in (0.0, 0.3, 0.7, 1.0):
        settings = UserSettings(user_id=1, temperature=t)
        assert settings.temperature == t

    for invalid in (-0.1, 0.1, 0.5, 1.2, 2.0):
        with pytest.raises(ValueError, match="Недопустимая температура"):
            UserSettings(user_id=1, temperature=invalid)


def test_chat_message_char_count():
    msg = ChatMessage(role=MessageRole.USER, content="Hello World!")

    assert msg.char_count == 12
    assert msg.role == MessageRole.USER
