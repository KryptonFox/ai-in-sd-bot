import pytest

from app.config import (
    DEFAULT_SCHEDULE_PATH,
    LEGACY_DEFAULT_SCHEDULE_PATH,
    ConfigError,
    Settings,
)
from app.core.limits import DEFAULT_AGENT_MAX_TOOL_CALLS, POSTGRES_INTEGER_MAX
from mcp_servers.student_tools.config import ServerSettings

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


def load_settings(tmp_path, **overrides: str) -> Settings:
    environment = {"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "unused"}
    environment.update(overrides)

    return Settings.load(tmp_path / "missing.env", environ=environment)


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


def test_agent_tool_call_budget_defaults_to_five(tmp_path) -> None:
    loaded = load_settings(tmp_path)
    direct = Settings(bot_token=TOKEN, postgres_password="unused")

    assert DEFAULT_AGENT_MAX_TOOL_CALLS == 5
    assert loaded.agent_max_tool_calls == 5
    assert direct.agent_max_tool_calls == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2", 2),
        ("6", 6),
        (str(POSTGRES_INTEGER_MAX), POSTGRES_INTEGER_MAX),
    ],
)
def test_agent_tool_call_budget_accepts_positive_postgres_integer(
    tmp_path, raw: str, expected: int
) -> None:
    settings = load_settings(tmp_path, AGENT_MAX_TOOL_CALLS=raw)

    assert settings.agent_max_tool_calls == expected


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "two", "2.5", str(POSTGRES_INTEGER_MAX + 1)],
)
def test_agent_tool_call_budget_rejects_zero_noninteger_and_overflow(tmp_path, raw: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_settings(tmp_path, AGENT_MAX_TOOL_CALLS=raw)

    assert str(error.value) == (
        f"AGENT_MAX_TOOL_CALLS: допустимый диапазон 1..{POSTGRES_INTEGER_MAX}."
    )
