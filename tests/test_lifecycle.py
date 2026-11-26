import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession

from app.config import Settings
from app.health import HealthState, start_health_server


async def test_http_health_returns_503_then_200(unused_tcp_port):
    pool = Mock(fetchval=AsyncMock(return_value=1))
    state = HealthState(pool=pool)
    runner = await start_health_server(state, unused_tcp_port)
    task = asyncio.create_task(asyncio.Event().wait())

    try:
        async with ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{unused_tcp_port}/health") as response:
                assert response.status == 503

            state.initialized, state.polling_task = True, task

            async with session.get(f"http://127.0.0.1:{unused_tcp_port}/health") as response:
                assert response.status == 200
                assert await response.json() == {
                    "status": "ok",
                    "database": "ok",
                    "polling": "running",
                }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


async def test_telegram_failure_closes_pool_and_session(monkeypatch):
    from app import __main__ as application

    pool = Mock(close=AsyncMock())
    bot = Mock(
        get_me=AsyncMock(side_effect=ConnectionError("proxy offline")),
        session=Mock(close=AsyncMock()),
    )
    monkeypatch.setattr(
        application,
        "create_pool",
        AsyncMock(return_value=pool),
    )
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))

    settings = Settings(bot_token="unused", postgres_password="unused")

    with pytest.raises(ConnectionError):
        await application.run(settings)

    pool.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_db_failure_closes_http_session(monkeypatch):
    from app import __main__ as application

    bot = Mock(session=Mock(close=AsyncMock()))
    monkeypatch.setattr(
        application,
        "create_pool",
        AsyncMock(side_effect=ConnectionError),
    )
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))

    with pytest.raises(ConnectionError):
        await application.run(Settings(bot_token="unused", postgres_password="unused"))

    bot.session.close.assert_awaited_once()


@pytest.mark.parametrize("failed_resource", ["mcp", "health", "llm", "bot", "database"])
async def test_shutdown_closes_remaining_resources_when_one_close_fails(failed_resource):
    from app.__main__ import _shutdown

    closed: list[str] = []

    def close_action(name: str):
        async def close() -> None:
            closed.append(name)
            if name == failed_resource:
                raise RuntimeError("close_failed")

        return AsyncMock(side_effect=close)

    pool = Mock(close=close_action("database"))
    bot = Mock(session=Mock(close=close_action("bot")))
    llm = Mock(aclose=close_action("llm"))
    runner = Mock(cleanup=close_action("health"))
    mcp = Mock(aclose=close_action("mcp"))
    state = HealthState(pool=pool, initialized=True)

    with pytest.raises(RuntimeError, match="close_failed"):
        await _shutdown(state, bot, llm, mcp, runner, None)

    assert closed == ["mcp", "health", "llm", "bot", "database"]
    assert not state.initialized


async def test_http_proxy_receives_connect_and_failure_has_no_direct_fallback(unused_tcp_port):
    from aiohttp_socks import ProxyError

    from app.telegram import create_bot

    requests = []

    async def proxy(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            requests.append(header)
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy, "127.0.0.1", unused_tcp_port)
    bot = create_bot(
        Settings(
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            postgres_password="unused",
            telegram_proxy_url=f"http://student:password@127.0.0.1:{unused_tcp_port}",
        )
    )

    try:
        with pytest.raises(ProxyError, match="407"):
            async with asyncio.timeout(5):
                await bot.get_me()

        assert requests[0].startswith(b"CONNECT api.telegram.org:443 HTTP/1.1\r\n")
        assert b"Proxy-Authorization: Basic " in requests[0]
    finally:
        await bot.session.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("failure", ["db", "telegram", "health", "polling", "none"])
async def test_application_closes_llm_at_every_lifecycle_boundary(monkeypatch, failure):
    from app import __main__ as application
    from app.mcp.connection import ToolClients

    mcp = Mock(start=AsyncMock(), recover=AsyncMock(), aclose=AsyncMock(), available=False)
    clients = ToolClients(student=mcp, math=None, composite=mcp)
    monkeypatch.setattr(application, "create_tool_clients", Mock(return_value=clients))
    llm = Mock(aclose=AsyncMock())
    monkeypatch.setattr(
        application,
        "PostgresTurnStore",
        Mock(return_value=Mock(recover=AsyncMock())),
    )
    pool = Mock(close=AsyncMock())
    bot = Mock(
        get_me=AsyncMock(return_value=Mock(username="test")),
        get_webhook_info=AsyncMock(return_value=Mock(url="")),
        session=Mock(close=AsyncMock()),
    )
    runner = Mock(cleanup=AsyncMock())
    dispatcher = Mock(start_polling=AsyncMock())
    monkeypatch.setattr(application, "OpenAIClient", Mock(return_value=llm))
    create_pool = AsyncMock(return_value=pool)
    start_health_server = AsyncMock(return_value=runner)
    monkeypatch.setattr(application, "create_pool", create_pool)
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))
    monkeypatch.setattr(
        application,
        "Dispatcher",
        Mock(return_value=dispatcher),
    )
    monkeypatch.setattr(application, "setup_bot_commands", AsyncMock())
    setup_bot_description = AsyncMock()
    monkeypatch.setattr(
        application,
        "setup_bot_description",
        setup_bot_description,
    )
    monkeypatch.setattr(application, "start_health_server", start_health_server)
    if failure == "db":
        create_pool.side_effect = RuntimeError
    elif failure == "telegram":
        bot.get_me.side_effect = RuntimeError
    elif failure == "health":
        start_health_server.side_effect = RuntimeError
    elif failure == "polling":
        dispatcher.start_polling.side_effect = RuntimeError

    if failure == "none":
        await application.run(Settings(bot_token="unused", postgres_password="unused"))
    else:
        with pytest.raises(RuntimeError):
            await application.run(Settings(bot_token="unused", postgres_password="unused"))

    llm.aclose.assert_awaited_once()
    if failure not in {"db", "telegram"}:
        mcp.aclose.assert_awaited_once()
        setup_bot_description.assert_awaited_once_with(bot)
    else:
        setup_bot_description.assert_not_awaited()

    bot.session.close.assert_awaited_once()
    if failure != "db":
        pool.close.assert_awaited_once()


async def test_run_wires_student_client_to_confirmations_and_composite_to_tools(
    monkeypatch,
):
    from app import __main__ as application
    from app.mcp.connection import ToolClients
    from app.mcp.restricted import RestrictedToolClient

    student = Mock(available=True)
    composite = Mock(
        start=AsyncMock(return_value=True),
        recover=AsyncMock(),
        aclose=AsyncMock(),
        available=True,
    )
    clients = ToolClients(student=student, math=None, composite=composite)
    monkeypatch.setattr(application, "create_tool_clients", Mock(return_value=clients))

    confirmations = Mock(recover=AsyncMock())
    configuration = Mock(return_value=confirmations)
    monkeypatch.setattr(application, "ConfirmationService", configuration)
    tools = Mock(available=True)
    tool_configuration = Mock(return_value=tools)
    monkeypatch.setattr(application, "ToolExecutionService", tool_configuration)
    dialog = Mock(recover=AsyncMock())
    dialog_configuration = Mock(return_value=dialog)
    monkeypatch.setattr(application, "DialogService", dialog_configuration)

    monkeypatch.setattr(application, "OpenAIClient", Mock(return_value=Mock(aclose=AsyncMock())))
    monkeypatch.setattr(application, "create_pool", AsyncMock(return_value=Mock(close=AsyncMock())))
    monkeypatch.setattr(
        application,
        "PostgresTurnStore",
        Mock(return_value=Mock(recover=AsyncMock())),
    )
    bot = Mock(
        get_me=AsyncMock(return_value=Mock(username="test")),
        get_webhook_info=AsyncMock(return_value=Mock(url="")),
        session=Mock(close=AsyncMock()),
    )
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))
    monkeypatch.setattr(application, "setup_bot_commands", AsyncMock())
    monkeypatch.setattr(application, "setup_bot_description", AsyncMock())
    dispatcher = Mock(
        start_polling=AsyncMock(side_effect=RuntimeError("stop after wiring")),
        resolve_used_update_types=Mock(return_value=[]),
    )
    monkeypatch.setattr(application, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(
        application,
        "start_health_server",
        AsyncMock(return_value=Mock(cleanup=AsyncMock())),
    )

    with pytest.raises(RuntimeError, match="stop after wiring"):
        await application.run(Settings(bot_token="unused", postgres_password="unused"))

    assert configuration.call_args.args[1] is student
    legacy_client = tool_configuration.call_args.args[0]
    assert isinstance(legacy_client, RestrictedToolClient)
    assert dialog_configuration.call_args.kwargs["tool_client"] is composite
    composite.start.assert_awaited_once()
    composite.aclose.assert_awaited_once()


async def test_recovery_isolates_confirmation_failure_from_math_recovery(monkeypatch):
    from app import __main__ as application

    confirmations = Mock(recover=AsyncMock(side_effect=RuntimeError("database busy")))
    mcp = Mock(recover=AsyncMock())

    async def stop_after_first_cycle(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(application.asyncio, "sleep", stop_after_first_cycle)

    with pytest.raises(asyncio.CancelledError):
        await application._recover_services(confirmations, mcp)

    confirmations.recover.assert_awaited_once()
    mcp.recover.assert_awaited_once()
