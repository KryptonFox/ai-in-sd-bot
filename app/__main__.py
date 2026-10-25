import argparse
import asyncio
import logging

from aiogram import Dispatcher

from app.config import ConfigError, Settings
from app.db.connection import create_pool
from app.handlers import router
from app.health import HealthState, start_health_server
from app.llm.client import OpenAIClient
from app.logging_setup import configure_logging
from app.services.dialog_service import DialogService
from app.telegram import create_bot, setup_bot_commands

logger = logging.getLogger("app")


async def run(settings: Settings) -> None:
    state = HealthState()
    bot = create_bot(settings)
    runner = None
    llm_client = None

    try:
        llm_client = OpenAIClient(settings)
        state.pool = await create_pool(settings)
        logger.info("PostgreSQL подключён: SELECT 1 выполнен, миграции применены.")

        async with asyncio.timeout(30):
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()

        if webhook.url:
            raise ConfigError(
                "У бота установлен webhook. Удалите его перед запуском polling "
                "или используйте отдельного учебного бота."
            )

        logger.info("Telegram доступен. Бот @%s запускает polling.", me.username)

        await setup_bot_commands(bot)

        dialog_service = DialogService(state.pool, settings, llm_client)

        dispatcher = Dispatcher()
        dispatcher.include_router(router)

        runner = await start_health_server(state, settings.health_port)
        state.polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                db=state.pool,
                dialog_service=dialog_service,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        )
        state.initialized = True

        await state.polling_task

    finally:
        state.initialized = False

        if state.polling_task and not state.polling_task.done():
            state.polling_task.cancel()
            await asyncio.gather(state.polling_task, return_exceptions=True)

        try:
            if runner:
                await runner.cleanup()
        finally:
            try:
                if llm_client:
                    await llm_client.aclose()
            finally:
                try:
                    await bot.session.close()
                finally:
                    if state.pool:
                        try:
                            async with asyncio.timeout(10):
                                await state.pool.close()
                        except TimeoutError:
                            state.pool.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Учебный текстовый эхо-бот")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    try:
        settings = Settings.load(args.env_file)
    except ConfigError as exc:
        print(str(exc))
        return 1

    configure_logging(settings)

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception:
        logger.exception("Не удалось запустить бот. Проверьте БД, токен и прокси.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
