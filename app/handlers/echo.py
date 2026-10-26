from aiogram import F, Router
from aiogram.types import Message

router = Router(name="echo")


@router.message(F.text)
async def echo_text(message: Message) -> None:
    if message.text is None:
        return

    await message.answer(message.text, parse_mode=None)
