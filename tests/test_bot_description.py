from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot

from app.telegram import BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, setup_bot_description


async def test_description_setup_updates_both_fields_within_telegram_limits() -> None:
    bot = MagicMock(spec=Bot)
    bot.set_my_description = AsyncMock(return_value=True)
    bot.set_my_short_description = AsyncMock(return_value=True)

    await setup_bot_description(bot)

    bot.set_my_description.assert_awaited_once_with(description=BOT_DESCRIPTION)
    bot.set_my_short_description.assert_awaited_once_with(short_description=BOT_SHORT_DESCRIPTION)
    assert 0 < len(BOT_DESCRIPTION) <= 512
    assert 0 < len(BOT_SHORT_DESCRIPTION) <= 120


@pytest.mark.parametrize("failed_method", ["set_my_description", "set_my_short_description"])
async def test_description_failure_does_not_block_other_field_or_expose_details(
    failed_method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = MagicMock(spec=Bot)
    bot.set_my_description = AsyncMock(return_value=True)
    bot.set_my_short_description = AsyncMock(return_value=True)
    getattr(bot, failed_method).side_effect = RuntimeError("PRIVATE DETAIL secret-token")

    await setup_bot_description(bot)

    bot.set_my_description.assert_awaited_once()
    bot.set_my_short_description.assert_awaited_once()
    assert "RuntimeError" in caplog.text
    assert "PRIVATE DETAIL" not in caplog.text
    assert "secret-token" not in caplog.text
