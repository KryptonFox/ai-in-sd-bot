import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.db.migrations.runner import (
    INIT_MIGRATIONS_TABLE_SQL,
    MIGRATIONS_DIR,
    compute_checksum,
    run_migrations,
)
from tests.conftest import DatabaseFixture


def test_migrations_directory_exists_and_has_files():
    assert MIGRATIONS_DIR.exists()

    sql_files = list(MIGRATIONS_DIR.glob("*.sql"))

    assert len(sql_files) >= 1
    assert any(f.name.startswith("0001_") for f in sql_files)


def test_compute_checksum():
    c1 = compute_checksum("SELECT 1;")
    c2 = compute_checksum("SELECT 1;")
    c3 = compute_checksum("SELECT 2;")

    assert c1 == c2
    assert c1 != c3
    assert len(c1) == 64


def test_migration_files_content_valid():
    for f in MIGRATIONS_DIR.glob("*.sql"):
        text = f.read_text(encoding="utf-8")

        assert len(text.strip()) > 0
        assert any(statement in text.upper() for statement in ("CREATE TABLE", "ALTER TABLE"))


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
)
async def test_migration_0005_preserves_saved_counters_and_relaxes_checks(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await asyncpg.create_pool(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        min_size=1,
        max_size=2,
    )
    event_id = "eval:before-budget-migration"
    confirmation_id = uuid4()
    confirmation_event_id = "eval:before-budget-migration-confirmation"
    expected_checksums: dict[int, str] = {}

    try:
        async with pool.acquire() as conn:
            await conn.execute(INIT_MIGRATIONS_TABLE_SQL)
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version > 4:
                    continue

                content = path.read_text(encoding="utf-8")
                checksum = compute_checksum(content)
                await conn.execute(content)
                await conn.execute(
                    """INSERT INTO schema_migrations(version,name,checksum)
                       VALUES($1,$2,$3)""",
                    version,
                    path.name,
                    checksum,
                )
                expected_checksums[version] = checksum

        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_events(user_id,event_id,calls_used)
                   VALUES($1,$2,$3)""",
                1,
                event_id,
                2,
            )
            await conn.execute(
                """INSERT INTO pending_confirmations
                   (id,user_id,event_id,position,arguments,timezone,idempotency_key,
                    expires_at,calls_remaining,request_id)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10)""",
                confirmation_id,
                1,
                confirmation_event_id,
                0,
                "{}",
                "UTC",
                f"reminder:{confirmation_id}",
                datetime.now(UTC) + timedelta(minutes=5),
                2,
                "request:before-budget-migration",
            )

        applied = await run_migrations(pool)

        assert applied == ["0005_agent_call_budget.sql"]
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT calls_used FROM agent_events WHERE event_id=$1",
                    event_id,
                )
                == 2
            )
            assert (
                await conn.fetchval(
                    "SELECT calls_remaining FROM pending_confirmations WHERE id=$1",
                    confirmation_id,
                )
                == 2
            )

            await conn.execute(
                "UPDATE agent_events SET calls_used=5 WHERE event_id=$1",
                event_id,
            )
            await conn.execute(
                "UPDATE pending_confirmations SET calls_remaining=5 WHERE id=$1",
                confirmation_id,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE agent_events SET calls_used=-1 WHERE event_id=$1",
                    event_id,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE pending_confirmations SET calls_remaining=-1 WHERE id=$1",
                    confirmation_id,
                )

            default = await conn.fetchval(
                """SELECT column_default FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='pending_confirmations'
                   AND column_name='calls_remaining'"""
            )
            assert default is None
            with pytest.raises(asyncpg.NotNullViolationError):
                await conn.execute(
                    """INSERT INTO pending_confirmations
                       (id,user_id,event_id,position,arguments,timezone,idempotency_key,
                        expires_at,request_id)
                       VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9)""",
                    uuid4(),
                    1,
                    "eval:no-default-confirmation",
                    0,
                    "{}",
                    "UTC",
                    f"reminder:{uuid4()}",
                    datetime.now(UTC) + timedelta(minutes=5),
                    "request:no-default-confirmation",
                )

            stored_checksums = {
                row["version"]: row["checksum"]
                for row in await conn.fetch(
                    "SELECT version,checksum FROM schema_migrations ORDER BY version"
                )
            }
            migration_0005 = MIGRATIONS_DIR / "0005_agent_call_budget.sql"
            expected_checksums[5] = compute_checksum(migration_0005.read_text(encoding="utf-8"))
            assert stored_checksums == expected_checksums

        assert await run_migrations(pool) == []
    finally:
        await pool.close()
