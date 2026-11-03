import pytest

from app.config import (
    DEFAULT_SCHEDULE_PATH,
    LEGACY_DEFAULT_SCHEDULE_PATH,
    ConfigError,
    Settings,
)
from mcp_servers.student_tools.config import ServerSettings


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


def test_settings_migrates_only_the_legacy_default_schedule_path(tmp_path) -> None:
    settings = Settings.load(
        tmp_path / "missing.env",
        environ={
            "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            "POSTGRES_PASSWORD": "unused",
            "SCHEDULE_PATH": LEGACY_DEFAULT_SCHEDULE_PATH,
        },
    )
    assert settings.schedule_path == DEFAULT_SCHEDULE_PATH


def test_settings_preserves_custom_schedule_path(tmp_path) -> None:
    custom_path = ".local/schedule.json"
    settings = Settings.load(
        tmp_path / "missing.env",
        environ={
            "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            "POSTGRES_PASSWORD": "unused",
            "SCHEDULE_PATH": custom_path,
        },
    )
    assert settings.schedule_path == custom_path


def test_server_settings_migrates_only_the_legacy_default_schedule_path(tmp_path) -> None:
    settings = ServerSettings.load(
        tmp_path / "missing.env",
        environ={
            "POSTGRES_PASSWORD": "unused",
            "MCP_SHARED_SECRET": "s" * 32,
            "SCHEDULE_PATH": LEGACY_DEFAULT_SCHEDULE_PATH,
        },
    )
    assert settings.schedule_path.as_posix() == DEFAULT_SCHEDULE_PATH


def test_server_settings_preserves_custom_schedule_path(tmp_path) -> None:
    custom_path = ".local/schedule.json"
    settings = ServerSettings.load(
        tmp_path / "missing.env",
        environ={
            "POSTGRES_PASSWORD": "unused",
            "MCP_SHARED_SECRET": "s" * 32,
            "SCHEDULE_PATH": custom_path,
        },
    )
    assert settings.schedule_path.as_posix() == custom_path
