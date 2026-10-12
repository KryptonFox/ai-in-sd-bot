import pytest

from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.user import UserSettings
from app.handlers.dialog import split_text


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


def test_split_text_under_limit():
    text = "Short message"

    chunks = split_text(text, max_chunk_size=4096)

    assert chunks == [text]


def test_split_text_over_limit():
    part1 = "a" * 3000
    part2 = "b" * 2000
    full_text = f"{part1}\n{part2}"

    chunks = split_text(full_text, max_chunk_size=4096)

    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2
    assert "\n".join(chunks) == full_text


def test_split_text_massive_paragraph_over_limit():
    massive = "x" * 10000

    chunks = split_text(massive, max_chunk_size=4096)

    assert len(chunks) == 3
    assert "".join(chunks) == massive
    for c in chunks:
        assert len(c) <= 4096
