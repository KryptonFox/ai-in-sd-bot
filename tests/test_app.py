import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import ConfigError, Settings
from app.health import HealthState, health_result
from app.logging_setup import SecretFilter
from app.telegram import create_bot

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD=secret-db\n", encoding="utf-8")

    return Settings.load(path, environ={})


def test_settings_environment_overrides_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD='p$a#ss'\nPOSTGRES_PORT=5432\n", encoding="utf-8"
    )

    config = Settings.load(path, environ={"POSTGRES_PORT": "55432"})

    assert config.postgres_port == 55432
    assert config.postgres_password == "p$a#ss"
    assert "p$a#ss" not in repr(config)
    assert TOKEN not in repr(config)


@pytest.mark.parametrize("value", ["abc", "0", "65536"])
def test_invalid_port_has_safe_error(tmp_path, value):
    path = tmp_path / ".env"

    with pytest.raises(ConfigError, match="POSTGRES_PORT"):
        Settings.load(
            path,
            environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "secret", "POSTGRES_PORT": value},
        )


@pytest.mark.parametrize("proxy", ["http://user:pass@localhost:8080", "socks5://localhost:1080"])
async def test_proxy_is_attached_to_bot_session(settings, proxy):
    from dataclasses import replace

    config = replace(settings, telegram_proxy_url=proxy)
    bot = create_bot(config)

    from aiogram.client.session.aiohttp import AiohttpSession

    assert isinstance(bot.session, AiohttpSession)
    assert bot.session._proxy == proxy

    await bot.session.close()


def test_bad_proxy_does_not_leak_credentials(tmp_path):
    proxy = "ftp://user:very-secret@host:123"

    with pytest.raises(ConfigError) as exc:
        Settings.load(
            tmp_path / ".env",
            environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "db", "TELEGRAM_PROXY_URL": proxy},
        )

    assert "TELEGRAM_PROXY_URL" in str(exc.value)
    assert "very-secret" not in str(exc.value)


async def test_health_tracks_pool_failure_and_recovery():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    task = asyncio.create_task(asyncio.Event().wait())
    state = HealthState(pool=pool, initialized=True, polling_task=task)

    try:
        assert (await health_result(state))[0] == 200
        pool.fetchval.assert_awaited_with("SELECT 1", timeout=3)

        pool.fetchval.side_effect = ConnectionError("secret-password")
        status, body = await health_result(state)
        assert status == 503
        assert body == {"status": "not_ready", "database": "unavailable", "polling": "running"}

        pool.fetchval.side_effect = None
        assert (await health_result(state))[0] == 200

        state.initialized = False
        assert (await health_result(state))[0] == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_health_without_polling_is_not_ready():
    state = HealthState(pool=None)

    status, body = await health_result(state)

    assert status == 503
    assert body["polling"] == "stopped"


def test_log_filter_redacts_secrets_and_exception():
    secret_filter = SecretFilter([TOKEN, "db-secret", "proxy-secret"])

    try:
        raise ValueError("proxy-secret")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            1,
            "Ошибка %s https://api.telegram.org/bot%s",
            ("db-secret", TOKEN),
            sys.exc_info(),
        )

    secret_filter.filter(record)
    rendered = logging.Formatter().format(record)

    assert all(secret not in rendered for secret in (TOKEN, "db-secret", "proxy-secret"))
    assert "ValueError" in rendered
