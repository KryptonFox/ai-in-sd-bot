from typing import Protocol

import asyncpg

from app.db.migrations.runner import run_migrations


class DatabaseSettings(Protocol):
    @property
    def postgres_host(self) -> str: ...

    @property
    def postgres_port(self) -> int: ...

    @property
    def postgres_db(self) -> str: ...

    @property
    def postgres_user(self) -> str: ...

    @property
    def postgres_password(self) -> str: ...


async def create_pool(settings: DatabaseSettings) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        min_size=1,
        max_size=5,
        timeout=5,
        command_timeout=5,
    )

    try:
        await pool.fetchval("SELECT 1", timeout=3)
        await run_migrations(pool)
    except BaseException:
        await pool.close()
        raise

    return pool
