import pytest

from app.config import ConfigError, Settings


@pytest.mark.parametrize(
    ("key", "minimum"),
    [
        ("HISTORY_MAX_MESSAGES", 1),
        ("HISTORY_MAX_CHARS", 100),
        ("DEFAULT_CONTEXT_LENGTH", 512),
        ("COMPACTION_THRESHOLD_MESSAGES", 2),
        ("COMPACTION_KEEP_RECENT", 1),
        ("USER_QUEUE_MAX_SIZE", 1),
    ],
)
@pytest.mark.parametrize("invalid", ["nan", "below_minimum"])
def test_numeric_settings_keep_validation_messages_and_limits(
    tmp_path,
    key: str,
    minimum: int,
    invalid: str,
) -> None:
    value = str(minimum - 1) if invalid == "below_minimum" else invalid

    with pytest.raises(ConfigError) as error:
        Settings.load(
            tmp_path / "missing.env",
            environ={
                "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                "POSTGRES_PASSWORD": "unused",
                key: value,
            },
        )

    assert str(error.value) == f"{key}: значение должно быть целым числом >= {minimum}."
