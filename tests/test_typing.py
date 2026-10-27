import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import Chat, Message

from app.core.exceptions import LLMTimeoutError
from app.core.schemas.agent import AgentResult
from app.handlers import dialog
from app.services.dialog_service import DialogService


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_typing_repeats_and_stops_with_generation(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
    ).as_(bot)
    refreshed = asyncio.Event()
    finish_generation = asyncio.Event()
    calls = 0

    async def send_action(**kwargs: object) -> bool:
        nonlocal calls
        assert kwargs["chat_id"] == 42
        assert kwargs["action"] == ChatAction.TYPING
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary Telegram failure")
        if calls >= 3:
            refreshed.set()
        return True

    async def generate(user_id: int, text: str) -> AgentResult:
        await finish_generation.wait()
        if outcome == "error":
            raise LLMTimeoutError("timeout")
        return AgentResult(text="Ответ")

    send_message = AsyncMock()
    monkeypatch.setattr(bot, "send_chat_action", send_action)
    monkeypatch.setattr(dialog, "TYPING_INTERVAL", 0.001)
    monkeypatch.setattr(dialog, "send_formatted_message", send_message)
    service = cast(
        DialogService,
        SimpleNamespace(process_user_message=generate, is_busy=AsyncMock(return_value=False)),
    )
    task = asyncio.create_task(dialog._execute_dialog(message, service, 42, "Вопрос"))
    try:
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            send_message.assert_not_awaited()
        else:
            finish_generation.set()
            await asyncio.wait_for(task, timeout=1)
            send_message.assert_awaited_once()
            response = send_message.call_args.args[1]
            if outcome == "error":
                assert "Превышено время ожидания" in response
            else:
                assert response == "Ответ"
        calls_at_finish = calls
        await asyncio.sleep(0.01)
        assert calls == calls_at_finish
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bot.session.close()
