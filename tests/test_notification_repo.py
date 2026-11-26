import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app.core.clock import FrozenClock
from app.db.connection import create_pool
from app.db.migrations.runner import (
    INIT_MIGRATIONS_TABLE_SQL,
    MIGRATIONS_DIR,
    compute_checksum,
    run_migrations,
)
from app.db.notification_records import (
    CityRecord,
    CitySelectionRecord,
    CitySelectionState,
    NotificationKind,
    NotificationState,
)
from app.db.repositories.notification_repo import PostgresNotificationRepository
from app.services.notifications.contracts import (
    BriefingContent,
    BriefingContext,
    BriefingSection,
)
from app.services.notifications.service import NotificationService
from tests.conftest import DatabaseFixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]

NOW = datetime(2031, 5, 1, 12, 0, tzinfo=UTC)
CITY = CityRecord(
    name="Казань",
    country="Россия",
    region="Татарстан",
    latitude=55.7963,
    longitude=49.1088,
)


async def add_user(pool: asyncpg.Pool, user_id: int, timezone: str = "Europe/Moscow") -> None:
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, timezone)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone
        """,
        user_id,
        timezone,
    )


async def add_reminder(
    pool: asyncpg.Pool,
    user_id: int,
    remind_at: datetime,
    text: str,
) -> None:
    await pool.execute(
        """
        INSERT INTO reminders (user_id, text, remind_at, timezone, idempotency_key)
        VALUES ($1, $2, $3, 'Europe/Moscow', $4)
        """,
        user_id,
        text,
        remind_at,
        f"test:{uuid.uuid4()}",
    )


async def test_reminder_event_preparation_is_idempotent_and_applies_window(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await add_user(pool, 43)
        await repository.remember_private_chat(42, 100, NOW)
        await add_reminder(pool, 42, NOW + timedelta(hours=1), "Будущее")
        await add_reminder(pool, 42, NOW - timedelta(minutes=5), "Свежее")
        await add_reminder(pool, 42, NOW - timedelta(minutes=15), "Ровно 15")
        await add_reminder(pool, 42, NOW - timedelta(minutes=16), "Старое")
        await add_reminder(pool, 43, NOW - timedelta(minutes=5), "Без адреса")

        first = await repository.prepare_reminder_events(NOW, timedelta(minutes=15))
        second = await repository.prepare_reminder_events(NOW, timedelta(minutes=15))

        assert first == 4
        assert second == 0

        rows = await pool.fetch(
            """
            SELECT event_key, state, last_error_code, payload
            FROM outgoing_notifications
            ORDER BY scheduled_for
            """
        )
        assert len(rows) == 4
        by_text = {json.loads(row["payload"])["text"]: row for row in rows}
        assert "Будущее" not in by_text
        assert by_text["Свежее"]["state"] == NotificationState.PENDING.value
        assert by_text["Свежее"]["last_error_code"] is None
        assert by_text["Ровно 15"]["state"] == NotificationState.PENDING.value
        assert by_text["Старое"]["state"] == NotificationState.MISSED.value
        assert by_text["Старое"]["last_error_code"] == "window_expired"
        assert by_text["Без адреса"]["state"] == NotificationState.SUPPRESSED.value
        assert by_text["Без адреса"]["last_error_code"] == "destination_unavailable"

        await add_reminder(pool, 42, NOW - timedelta(minutes=1), "Конкурентное")
        concurrent = await asyncio.gather(
            repository.prepare_reminder_events(NOW, timedelta(minutes=15)),
            repository.prepare_reminder_events(NOW, timedelta(minutes=15)),
        )
        assert sum(concurrent) == 1
        count = await pool.fetchval(
            "SELECT count(*) FROM outgoing_notifications WHERE payload->>'text' = $1",
            "Конкурентное",
        )
        assert count == 1
    finally:
        await pool.close()


async def test_concurrent_claims_lease_single_event_and_recover(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)
        await add_reminder(pool, 42, NOW - timedelta(minutes=1), "Задание")
        assert await repository.prepare_reminder_events(NOW, timedelta(minutes=15)) == 1

        first, second = await asyncio.gather(
            repository.claim_due_event(NOW, NOW + timedelta(seconds=30), 3),
            repository.claim_due_event(NOW, NOW + timedelta(seconds=30), 3),
        )

        claimed = [job for job in (first, second) if job is not None]
        assert len(claimed) == 1
        job = claimed[0]
        assert job.kind is NotificationKind.REMINDER
        assert job.attempt_count == 1
        assert job.lease_id is not None
        assert job.state is NotificationState.SENDING

        assert await repository.claim_due_event(NOW, NOW + timedelta(seconds=30), 3) is None

        recovered = await repository.recover_expired_leases(
            NOW + timedelta(seconds=31),
            3,
        )
        assert recovered == 1
        row = await pool.fetchrow("SELECT * FROM outgoing_notifications")
        assert row is not None
        assert row["state"] == NotificationState.RETRY.value
        assert row["last_error_code"] == "lease_expired"

        recovered_job = await repository.claim_due_event(
            NOW + timedelta(seconds=31),
            NOW + timedelta(seconds=61),
            3,
        )
        assert recovered_job is not None
        assert recovered_job.attempt_count == 2

        await repository.mark_sent(
            recovered_job.id,
            recovered_job.lease_id,
            NOW + timedelta(seconds=32),
        )
        row = await pool.fetchrow("SELECT * FROM outgoing_notifications")
        assert row is not None
        assert row["state"] == NotificationState.SENT.value
        assert row["sent_at"] is not None
    finally:
        await pool.close()


async def test_expired_lease_at_attempt_limit_fails(database: DatabaseFixture) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)
        await add_reminder(pool, 42, NOW, "Предел")
        await repository.prepare_reminder_events(NOW, timedelta(minutes=15))
        await pool.execute(
            """
            UPDATE outgoing_notifications
            SET state = 'sending', attempt_count = 3,
                lease_id = gen_random_uuid(), lease_until = $1
            """,
            NOW - timedelta(seconds=1),
        )

        assert await repository.recover_expired_leases(NOW, 3) == 1

        row = await pool.fetchrow("SELECT state, last_error_code FROM outgoing_notifications")
        assert row is not None
        assert row["state"] == NotificationState.FAILED.value
        assert row["last_error_code"] == "lease_expired"
    finally:
        await pool.close()


async def test_city_selection_owner_tamper_expiry_and_confirmation(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)
        selection = CitySelectionRecord(
            id=uuid.uuid4(),
            user_id=42,
            candidates=(CITY,),
            state=CitySelectionState.PENDING,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            selected_index=None,
        )
        await repository.create_city_selection(selection)

        assert await repository.confirm_city_selection(selection.id, 43, 0, NOW) is None
        assert (
            await pool.fetchval(
                "SELECT state FROM notification_city_selections WHERE id=$1",
                selection.id,
            )
            == CitySelectionState.PENDING.value
        )

        confirmed = await repository.confirm_city_selection(selection.id, 42, 0, NOW)
        assert confirmed is not None
        assert confirmed.city == CITY
        assert confirmed.destination_chat_id == 100

        stored = await pool.fetchrow(
            "SELECT state, selected_index FROM notification_city_selections WHERE id=$1",
            selection.id,
        )
        assert stored is not None
        assert stored["state"] == CitySelectionState.CONFIRMED.value
        assert stored["selected_index"] == 0

        expired = CitySelectionRecord(
            id=uuid.uuid4(),
            user_id=42,
            candidates=(CITY,),
            state=CitySelectionState.PENDING,
            created_at=NOW - timedelta(minutes=10),
            expires_at=NOW - timedelta(seconds=1),
            selected_index=None,
        )
        await repository.create_city_selection(expired)
        assert await repository.confirm_city_selection(expired.id, 42, 0, NOW) is None
        assert (
            await pool.fetchval(
                "SELECT state FROM notification_city_selections WHERE id=$1",
                expired.id,
            )
            == CitySelectionState.EXPIRED.value
        )

        cancellable = CitySelectionRecord(
            id=uuid.uuid4(),
            user_id=42,
            candidates=(CITY,),
            state=CitySelectionState.PENDING,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            selected_index=None,
        )
        await repository.create_city_selection(cancellable)
        assert await repository.cancel_city_selection(cancellable.id, 43, NOW) is False
        assert await repository.cancel_city_selection(cancellable.id, 42, NOW) is True
    finally:
        await pool.close()


async def test_briefing_event_is_unique_and_requires_settings(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)

        assert await repository.set_briefing_enabled(42, True, NOW) is None

        await pool.execute(
            """
            UPDATE notification_settings
            SET city_name='Казань', city_country='Россия', city_region='Татарстан',
                city_latitude=55.7963, city_longitude=49.1088, city_confirmed_at=$1
            WHERE user_id=42
            """,
            NOW,
        )
        enabled = await repository.set_briefing_enabled(42, True, NOW)
        assert enabled is not None
        assert enabled.briefing_enabled

        candidates = await repository.briefing_candidates()
        assert [candidate.user_id for candidate in candidates] == [42]

        local_date = date(2031, 5, 1)
        trace_id = uuid.uuid4()
        first = await repository.create_briefing_event(
            42,
            local_date,
            "Europe/Moscow",
            datetime(2031, 5, 1, 5, 0, tzinfo=UTC),
            trace_id,
            NOW,
        )
        second = await repository.create_briefing_event(
            42,
            local_date,
            "Europe/Moscow",
            datetime(2031, 5, 1, 5, 0, tzinfo=UTC),
            trace_id,
            NOW,
        )
        assert first is True
        assert second is False

        row = await pool.fetchrow("SELECT * FROM outgoing_notifications")
        assert row is not None
        assert row["event_key"] == "briefing:42:2031-05-01"
        assert row["kind"] == NotificationKind.DAILY_BRIEFING.value
        assert row["trace_id"] == trace_id

        assert await repository.set_briefing_enabled(42, False, NOW) is not None
        assert (
            await repository.create_briefing_event(
                42,
                date(2031, 5, 2),
                "Europe/Moscow",
                datetime(2031, 5, 2, 5, 0, tzinfo=UTC),
                uuid.uuid4(),
                NOW,
            )
            is False
        )
    finally:
        await pool.close()


async def test_real_tick_delivers_reminder_once_and_sets_trace(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)
    messages: list[tuple[int, str]] = []
    traces = []

    async def deliver(chat_id: int, text: str) -> None:
        messages.append((chat_id, text))

    async def briefing(context: BriefingContext) -> BriefingContent:
        return BriefingContent(sections=(BriefingSection(title="Занятия", text="Математика"),))

    async def trace_sink(trace) -> None:
        traces.append(trace)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)
        await add_reminder(pool, 42, NOW - timedelta(minutes=2), "Сдать отчёт")
        service = NotificationService(
            repository,
            clock=FrozenClock(NOW),
            deliver=deliver,
            briefing=briefing,
            city_resolver=_EmptyResolver(),
            trace_sink=trace_sink,
        )

        first = await service.tick()
        second = await service.tick()

        assert first.prepared_reminders == 1
        assert first.sent == 1
        assert second.prepared_reminders == 0
        assert second.sent == 0
        assert len(messages) == 1
        assert messages[0][0] == 100
        assert "Сдать отчёт" in messages[0][1]
        assert "задержка 2 мин." in messages[0][1]
        assert len(traces) == 1
        assert traces[0].route == "reminder"
        assert [step.component for step in traces[0].steps] == [
            "notification",
            "telegram",
        ]
        assert await pool.fetchval("SELECT count(*) FROM outgoing_notifications") == 1
        assert (
            await pool.fetchval("SELECT state FROM outgoing_notifications")
            == NotificationState.SENT.value
        )
    finally:
        await pool.close()


async def test_repository_marks_terminal_only_for_matching_lease(
    database: DatabaseFixture,
) -> None:
    settings, *_ = database
    pool = await create_pool(settings)
    repository = PostgresNotificationRepository(pool)

    try:
        await add_user(pool, 42)
        await repository.remember_private_chat(42, 100, NOW)
        await add_reminder(pool, 42, NOW, "Проверка аренды")
        await repository.prepare_reminder_events(NOW, timedelta(minutes=15))
        job = await repository.claim_due_event(NOW, NOW + timedelta(seconds=30), 3)
        assert job is not None

        assert (
            await repository.mark_terminal(
                job.id,
                uuid.uuid4(),
                NotificationState.FAILED,
                "wrong_lease",
                NOW,
            )
            is False
        )
        assert await repository.mark_terminal(
            job.id,
            job.lease_id,
            NotificationState.SUPPRESSED,
            "destination_unavailable",
            NOW,
        )
        row = await pool.fetchrow("SELECT state, last_error_code FROM outgoing_notifications")
        assert row is not None
        assert row["state"] == NotificationState.SUPPRESSED.value
        assert row["last_error_code"] == "destination_unavailable"
    finally:
        await pool.close()


async def test_migration_0007_upgrades_lab2_database_and_is_repeatable(
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

    try:
        async with pool.acquire() as conn:
            await conn.execute(INIT_MIGRATIONS_TABLE_SQL)
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version > 5:
                    continue

                content = path.read_text(encoding="utf-8")
                await conn.execute(content)
                await conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum)
                    VALUES ($1, $2, $3)
                    """,
                    version,
                    path.name,
                    compute_checksum(content),
                )

            await conn.execute(
                "INSERT INTO user_settings (user_id, timezone) VALUES (42, 'Europe/Moscow')"
            )
            await conn.execute(
                """
                INSERT INTO reminders (user_id, text, remind_at, timezone, idempotency_key)
                VALUES (42, 'До миграции', $1, 'Europe/Moscow', 'lab2:reminder')
                """,
                NOW,
            )

        applied = await run_migrations(pool)

        assert "0007_notifications.sql" in applied
        assert await pool.fetchval("SELECT text FROM reminders WHERE user_id=42") == "До миграции"
        assert await pool.fetchval("SELECT count(*) FROM notification_settings") == 0
        assert await pool.fetchval("SELECT count(*) FROM outgoing_notifications") == 0
        assert (
            await pool.fetchval(
                """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema='public'
              AND table_name IN (
                  'notification_settings',
                  'notification_city_selections',
                  'outgoing_notifications'
              )
            """
            )
            == 3
        )
        assert await run_migrations(pool) == []
    finally:
        await pool.close()


class _EmptyResolver:
    async def resolve(self, query: str) -> tuple[CityRecord, ...]:
        return ()
