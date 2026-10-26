from aiogram import Router

from app.handlers.agent_commands import router as agent_commands_router
from app.handlers.commands import router as commands_router
from app.handlers.dialog import router as dialog_router

router = Router(name="main_router")
router.include_router(commands_router)
router.include_router(agent_commands_router)
router.include_router(dialog_router)

__all__ = ["commands_router", "dialog_router", "router"]
