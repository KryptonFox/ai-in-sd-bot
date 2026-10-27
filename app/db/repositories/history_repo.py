import json

import asyncpg

from app.core.schemas.message import ChatMessage, MessageRole


async def add_dialog_message(pool: asyncpg.Pool, user_id: int, message: ChatMessage) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )

        metadata_json = json.dumps(message.metadata)
        await conn.execute(
            """
            INSERT INTO dialog_messages (user_id, role, content, char_count, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            user_id,
            str(message.role),
            message.content,
            message.char_count,
            metadata_json,
        )


async def get_dialog_history(
    pool: asyncpg.Pool, user_id: int, max_messages: int, max_chars: int
) -> list[ChatMessage]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, metadata, char_count
            FROM dialog_messages
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            user_id,
            max_messages,
        )

    if not rows:
        return []

    selected: list[ChatMessage] = []
    total_chars = 0

    for row in rows:
        char_count = row["char_count"]
        if total_chars + char_count > max_chars:
            break

        total_chars += char_count
        raw_meta = row["metadata"]
        metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta or {}

        selected.append(
            ChatMessage(role=MessageRole(row["role"]), content=row["content"], metadata=metadata)
        )

    selected.reverse()
    return selected


async def clear_dialog_history(pool: asyncpg.Pool, user_id: int) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM dialog_messages WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM dialog_summaries WHERE user_id = $1", user_id)


async def get_dialog_summary(pool: asyncpg.Pool, user_id: int) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary FROM dialog_summaries WHERE user_id = $1", user_id
        )
    return row["summary"] if row else None


async def set_dialog_summary(pool: asyncpg.Pool, user_id: int, summary: str) -> None:
    async with pool.acquire() as conn:
        await _set_summary(conn, user_id, summary)


async def _set_summary(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, user_id: int, summary: str
) -> None:
    await conn.execute(
        "INSERT INTO dialog_summaries (user_id, summary, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW()",
        user_id,
        summary,
    )


async def commit_compaction(
    pool: asyncpg.Pool, user_id: int, message_ids: list[int], summary: str
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _set_summary(conn, user_id, summary)
        await conn.execute(
            "DELETE FROM dialog_messages WHERE user_id=$1 AND id=ANY($2)", user_id, message_ids
        )


async def clear_dialog_summary(pool: asyncpg.Pool, user_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dialog_summaries WHERE user_id=$1", user_id)


async def get_user_messages_with_ids(
    pool: asyncpg.Pool, user_id: int, limit: int = 64
) -> list[tuple[int, ChatMessage]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, content, metadata, char_count
            FROM dialog_messages
            WHERE user_id = $1
            ORDER BY id ASC LIMIT $2
            """,
            user_id,
            limit,
        )

    result: list[tuple[int, ChatMessage]] = []
    for row in rows:
        raw_meta = row["metadata"]
        metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta or {}
        msg = ChatMessage(role=MessageRole(row["role"]), content=row["content"], metadata=metadata)
        result.append((row["id"], msg))
    return result


async def delete_dialog_messages_by_ids(pool: asyncpg.Pool, message_ids: list[int]) -> None:
    if not message_ids:
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dialog_messages WHERE id = ANY($1)", message_ids)
