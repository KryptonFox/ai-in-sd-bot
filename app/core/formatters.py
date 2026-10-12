import html
import logging
import re

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger("app.core.formatters")


def render_markdown(text: str) -> str:
    if not text:
        return ""

    code_blocks: list[str] = []

    def _extract_code_block(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        code = match.group(2)
        escaped_code = html.escape(code)
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
        else:
            tag = f"<pre><code>{escaped_code}</code></pre>"
        code_blocks.append(tag)
        return f"\x00CB{len(code_blocks) - 1}\x00"

    processed = re.sub(
        "```([a-zA-Z0-9_\\+\\-]+)?\\n?(.*?)```", _extract_code_block, text, flags=re.DOTALL
    )

    inline_codes: list[str] = []

    def _extract_inline_code(match: re.Match[str]) -> str:
        escaped = html.escape(match.group(1))
        inline_codes.append(f"<code>{escaped}</code>")
        return f"\x00IC{len(inline_codes) - 1}\x00"

    processed = re.sub("`([^`\\n]+)`", _extract_inline_code, processed)
    processed = html.escape(processed)

    processed = re.sub("(?m)^(#{1,6})\\s+(.+?)$", "<b>\\2</b>", processed)
    processed = re.sub(
        "\\[([^\\]]+)\\]\\((https?://[^\\s\\)]+)\\)", '<a href="\\2">\\1</a>', processed
    )
    processed = re.sub("\\*\\*\\*(.+?)\\*\\*\\*", "<b><i>\\1</i></b>", processed, flags=re.DOTALL)
    processed = re.sub("\\*\\*(.+?)\\*\\*", "<b>\\1</b>", processed, flags=re.DOTALL)
    processed = re.sub("__(.+?)__", "<b>\\1</b>", processed, flags=re.DOTALL)
    processed = re.sub("(?<!\\*)\\*([^\\*\\n]+)\\*(?!\\*)", "<i>\\1</i>", processed)
    processed = re.sub("(?<!\\w)_([^_\\n]+)_(?!\\w)", "<i>\\1</i>", processed)
    processed = re.sub("~~([^~\\n]+)~~", "<s>\\1</s>", processed)
    processed = re.sub("(?m)^(&gt;|>)\\s*(.+?)$", "<blockquote>\\2</blockquote>", processed)

    for idx, snippet in enumerate(inline_codes):
        processed = processed.replace(f"\x00IC{idx}\x00", snippet)

    for idx, snippet in enumerate(code_blocks):
        processed = processed.replace(f"\x00CB{idx}\x00", snippet)

    return processed


async def send_formatted_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    as_reply: bool = False,
) -> Message:
    html_text = render_markdown(text)
    send_fn = message.reply if as_reply else message.answer

    try:
        return await send_fn(html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        logger.warning("Ошибка рендера HTML в Telegram (%s), fallback на plain text", exc)
        try:
            return await send_fn(text, parse_mode=None, reply_markup=reply_markup)
        except TelegramBadRequest:
            if as_reply:
                return await message.answer(text, parse_mode=None, reply_markup=reply_markup)
            raise
