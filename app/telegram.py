import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.config import Settings
from app.modes.registry import ModeRegistry, registry

logger = logging.getLogger("app.telegram")


def create_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.telegram_proxy_url or None, timeout=40)

    return Bot(token=settings.bot_token, session=session)


def get_default_bot_commands(mode_registry: ModeRegistry = registry) -> list[BotCommand]:
    commands: list[BotCommand] = [
        BotCommand(command="start", description="Главное меню и выбор роли"),
    ]

    for mode in mode_registry.list_modes():
        desc = f"{mode.name}: {mode.description}"[:256]
        commands.append(BotCommand(command=mode.command, description=desc))

    commands.extend(
        [
            BotCommand(command="settings", description="Настройки режима и температуры"),
            BotCommand(command="reset", description="Очистить историю диалога"),
            BotCommand(command="help", description="Справка по доступным командам"),
            BotCommand(command="tools", description="Доступные инструменты MCP"),
            BotCommand(command="timezone", description="Часовой пояс IANA для напоминаний"),
            BotCommand(command="why", description="Последнее решение агента"),
        ]
    )

    return commands


async def setup_bot_commands(bot: Bot, mode_registry: ModeRegistry = registry) -> list[BotCommand]:
    commands = get_default_bot_commands(mode_registry)
    try:
        await bot.set_my_commands(commands=commands, scope=BotCommandScopeDefault())
        logger.info("Зарегистрировано %d команд в автокомплите Telegram.", len(commands))
    except Exception as exc:
        logger.warning("Не удалось зарегистрировать команды в автокомплите Telegram: %s", exc)

    return commands
