import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

import asyncpg
from aiogram import Bot, Dispatcher
from aiohttp import web

from app.config import ConfigError, Settings
from app.db.connection import create_pool
from app.db.repositories.turn_repo import PostgresTurnStore
from app.handlers import router
from app.health import HealthState, start_health_server
from app.llm.client import OpenAIClient
from app.logging_setup import configure_logging
from app.mcp.client import MCPClient
from app.mcp.connection import create_student_tools_client
from app.services.agent_service import AgentService
from app.services.confirmation_service import ConfirmationService
from app.services.dialog_service import DialogService
from app.services.tool_execution import ToolExecutionService
from app.telegram import create_bot, setup_bot_commands, setup_bot_description

logger = logging.getLogger("app")


async def run(settings: Settings) -> None:
    state = HealthState()
    bot = create_bot(settings)
    runner = None
    llm_client = None
    mcp_client = None
    recovery_task = None

    try:
        llm_client = OpenAIClient(settings)
        state.pool = await create_pool(settings)
        logger.info("PostgreSQL подключён: SELECT 1 выполнен, миграции применены.")

        await _prepare_telegram(bot)

        mcp_client = create_student_tools_client(settings)
        await mcp_client.start()
        state.mcp = mcp_client

        confirmations = ConfirmationService(
            state.pool,
            mcp_client,
            ttl=settings.confirmation_ttl,
        )
        store = PostgresTurnStore(state.pool)

        tools = ToolExecutionService(
            mcp_client,
            confirmations,
            store,
            max_calls=settings.agent_max_tool_calls,
            max_output_chars=settings.tool_result_max_chars,
        )

        agent = AgentService(
            llm_client,
            tools,
            max_llm_steps=settings.agent_max_llm_steps,
        )
        dialog_service = DialogService(
            state.pool,
            settings,
            llm_client,
            agent=agent,
            confirmations=confirmations,
            tool_client=mcp_client,
            store=store,
        )
        await dialog_service.recover()

        recovery_task = asyncio.create_task(_recover_services(confirmations, mcp_client))

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
        await _shutdown(
            state,
            bot,
            llm_client,
            mcp_client,
            runner,
            recovery_task,
        )


async def _prepare_telegram(bot: Bot) -> None:
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
    await setup_bot_description(bot)


async def _recover_services(
    confirmations: ConfirmationService,
    mcp: MCPClient,
) -> None:
    while True:
        try:
            await confirmations.recover()
            if not mcp.available:
                await mcp.start()
        except Exception:
            logger.warning("agent_recovery_unavailable")

        await asyncio.sleep(15)


async def _close_database(pool: asyncpg.Pool) -> None:
    try:
        async with asyncio.timeout(10):
            await pool.close()
    except TimeoutError:
        pool.terminate()


async def _shutdown(
    state: HealthState,
    bot: Bot,
    llm: OpenAIClient | None,
    mcp: MCPClient | None,
    runner: web.AppRunner | None,
    recovery_task: asyncio.Task[None] | None,
) -> None:
    state.initialized = False

    async with AsyncExitStack() as cleanup:
        if state.pool is not None:
            cleanup.push_async_callback(_close_database, state.pool)

        cleanup.push_async_callback(bot.session.close)
        if llm is not None:
            cleanup.push_async_callback(llm.aclose)

        if runner is not None:
            cleanup.push_async_callback(runner.cleanup)

        if mcp is not None:
            cleanup.push_async_callback(mcp.aclose)

        for task in (state.polling_task, recovery_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


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
