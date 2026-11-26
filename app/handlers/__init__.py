from aiogram import Router

from app.handlers.agent_commands import router as agent_commands_router
from app.handlers.calendar_import import router as calendar_import_router
from app.handlers.commands import router as commands_router
from app.handlers.dialog import router as dialog_router
from app.handlers.notifications import router as notifications_router
from app.handlers.trace import router as trace_router

router = Router(name="main_router")
router.include_router(commands_router)
router.include_router(agent_commands_router)
router.include_router(calendar_import_router)
router.include_router(notifications_router)
router.include_router(trace_router)
router.include_router(dialog_router)

__all__ = [
    "calendar_import_router",
    "commands_router",
    "dialog_router",
    "notifications_router",
    "router",
    "trace_router",
]
