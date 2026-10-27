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
    monkeypatch.setattr(application, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))

    settings = Settings(bot_token="unused", postgres_password="unused")

    with pytest.raises(ConnectionError):
        await application.run(settings)

    pool.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_db_failure_closes_http_session(monkeypatch):
    from app import __main__ as application

    bot = Mock(session=Mock(close=AsyncMock()))
    monkeypatch.setattr(application, "create_pool", AsyncMock(side_effect=ConnectionError))
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))

    with pytest.raises(ConnectionError):
        await application.run(Settings(bot_token="unused", postgres_password="unused"))

    bot.session.close.assert_awaited_once()


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

    mcp = Mock(start=AsyncMock(), aclose=AsyncMock(), available=False)
    monkeypatch.setattr(application, "MCPClient", Mock(return_value=mcp))
    llm = Mock(aclose=AsyncMock())
    monkeypatch.setattr(
        application, "PostgresTurnStore", Mock(return_value=Mock(recover=AsyncMock()))
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
    monkeypatch.setattr(application, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(application, "setup_bot_commands", AsyncMock())
    setup_bot_description = AsyncMock()
    monkeypatch.setattr(application, "setup_bot_description", setup_bot_description)
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
