import asyncpg

from app.core.schemas.user import UserSettings


async def get_or_create_user(pool: asyncpg.Pool, user_id: int) -> UserSettings:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )

        row = await conn.fetchrow(
            """
            SELECT user_id, mode, temperature, created_at, updated_at
            FROM user_settings
            WHERE user_id = $1
            """,
            user_id,
        )

        if not row:
            raise RuntimeError(f"Не удалось получить или создать пользователя {user_id}")

        return UserSettings(
            user_id=row["user_id"],
            mode=row["mode"],
            temperature=row["temperature"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


async def set_user_mode(pool: asyncpg.Pool, user_id: int, mode: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, mode, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET mode = EXCLUDED.mode,
                updated_at = NOW()
            """,
            user_id,
            mode,
        )


async def set_user_temperature(pool: asyncpg.Pool, user_id: int, temperature: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, temperature, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET temperature = EXCLUDED.temperature,
                updated_at = NOW()
            """,
            user_id,
            temperature,
        )
