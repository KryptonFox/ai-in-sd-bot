from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg


async def get_timezone(pool: asyncpg.Pool, user_id: int) -> str | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT timezone FROM user_settings WHERE user_id = $1",
            user_id,
        )


async def set_timezone(pool: asyncpg.Pool, user_id: int, timezone: str) -> None:
    if not isinstance(timezone, str):
        raise ValueError("Некорректная временная зона")

    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Некорректная временная зона") from exc

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, timezone, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET timezone = EXCLUDED.timezone,
                updated_at = NOW()
            """,
            user_id,
            timezone,
        )
