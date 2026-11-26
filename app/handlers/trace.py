from uuid import UUID

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.clock import SystemClock
from app.db.repositories.trace_repo import find_trace

router = Router(name="trace")


@router.message(Command("trace"), F.chat.type == "private")
async def handle_trace(message: Message, db: asyncpg.Pool) -> None:
    owner = message.from_user
    if owner is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    selector = parts[1].strip() if len(parts) == 2 else "last"
    try:
        trace_id = None if selector == "last" else UUID(selector)
    except ValueError:
        await message.answer("Трасса не найдена.")
        return

    trace = await find_trace(db, owner.id, SystemClock().now(), trace_id)
    if trace is None:
        await message.answer("Трасса не найдена.")
        return

    lines = [f"Трасса {trace.id}"]
    if trace.route is not None:
        lines.append(f"Маршрут {trace.route}")
    for step in trace.steps:
        details = " · ".join(
            value for value in (step.tool_name, step.model_name, step.error_code) if value
        )
        line = f"{step.component} {step.action} · {step.duration_ms:.0f} мс · {step.status}"
        lines.append(f"{line} · {details}" if details else line)
    await message.answer("\n".join(lines))
