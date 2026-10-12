import re
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import BotCommandScopeDefault

from app.modes.base import BotMode
from app.modes.registry import ModeRegistry
from app.telegram import get_default_bot_commands, setup_bot_commands


def test_get_default_bot_commands_structure():
    commands = get_default_bot_commands()

    assert len(commands) >= 6
    cmd_names = [c.command for c in commands]

    assert "start" in cmd_names
    assert "study" in cmd_names
    assert "translate" in cmd_names
    assert "review" in cmd_names
    assert "settings" in cmd_names
    assert "reset" in cmd_names
    assert "help" in cmd_names

    for cmd in commands:
        assert re.match(r"^[a-z0-9_]{1,32}$", cmd.command)
        assert 1 <= len(cmd.description) <= 256


def test_get_default_bot_commands_custom_registry():
    reg = ModeRegistry()
    reg.register(
        BotMode(
            id="custom_role",
            command="custom_role",
            name="Кастомная роль",
            description="Тестовое описание роли",
            system_prompt="Тест",
        )
    )

    commands = get_default_bot_commands(reg)
    cmd_names = [c.command for c in commands]

    assert "custom_role" in cmd_names
    custom_cmd = next(c for c in commands if c.command == "custom_role")
    assert "Кастомная роль" in custom_cmd.description


async def test_setup_bot_commands_success():
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(return_value=True)

    commands = await setup_bot_commands(bot)

    assert len(commands) >= 6
    bot.set_my_commands.assert_awaited_once()

    call_args = bot.set_my_commands.call_args
    assert call_args.kwargs["commands"] == commands
    assert isinstance(call_args.kwargs["scope"], BotCommandScopeDefault)


async def test_setup_bot_commands_graceful_error_handling():
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=RuntimeError("Telegram API error"))

    commands = await setup_bot_commands(bot)

    assert len(commands) >= 6
    bot.set_my_commands.assert_awaited_once()
