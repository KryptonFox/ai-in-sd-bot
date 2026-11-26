from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.services.notifications.contracts import NotificationDeliveryError


class TelegramBotDelivery:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except TelegramRetryAfter as error:
            raise NotificationDeliveryError(
                "telegram_retry_after",
                retryable=True,
                retry_after=float(error.retry_after),
            ) from error
        except (TelegramNetworkError, TelegramServerError) as error:
            raise NotificationDeliveryError(
                "telegram_network",
                retryable=True,
            ) from error
        except TelegramAPIError as error:
            raise NotificationDeliveryError(
                "telegram_api_error",
                retryable=False,
            ) from error
        except Exception as error:
            raise NotificationDeliveryError(
                "telegram_error",
                retryable=True,
            ) from error
