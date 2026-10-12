import hashlib
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger("app.migrations")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "sql"
INIT_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.info("Файлы миграций не найдены в %s", MIGRATIONS_DIR)
        return []

    applied_names: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtext('app_migrations'))")
        try:
            await conn.execute(INIT_MIGRATIONS_TABLE_SQL)
            rows = await conn.fetch(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC"
            )
            applied_map = {row["version"]: (row["name"], row["checksum"]) for row in rows}

            for file_path in migration_files:
                parts = file_path.name.split("_", 1)
                try:
                    version = int(parts[0])
                except ValueError:
                    logger.warning(
                        "Пропуск файла с некорректным номером версии: %s", file_path.name
                    )
                    continue

                content = file_path.read_text(encoding="utf-8")
                checksum = compute_checksum(content)

                if version in applied_map:
                    prev_name, prev_checksum = applied_map[version]
                    if prev_checksum != checksum:
                        msg = (
                            f"Нарушена целостность миграции {file_path.name}! "
                            f"Контрольная сумма в базе ({prev_checksum}) не совпадает "
                            f"с файлом ({checksum}). Не изменяйте уже примененные миграции."
                        )
                        raise RuntimeError(msg)
                    continue

                logger.info("Применение миграции: %s (версия %d)...", file_path.name, version)
                async with conn.transaction():
                    await conn.execute(content)
                    await conn.execute(
                        """
                        INSERT INTO schema_migrations (version, name, checksum)
                        VALUES ($1, $2, $3)
                        """,
                        version,
                        file_path.name,
                        checksum,
                    )

                applied_names.append(file_path.name)
                logger.info("Миграция %s успешно применена.", file_path.name)

        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('app_migrations'))")

    return applied_names
