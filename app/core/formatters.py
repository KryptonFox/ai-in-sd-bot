import logging
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, MessageEntity
from telegramify_markdown import RenderConfig, convert, split_entities
from telegramify_markdown.entity import MessageEntity as ConvertedEntity

logger = logging.getLogger("app.core.formatters")
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_ALLOWED_ENTITIES = frozenset({"bold", "italic", "code", "pre", "text_link"})
_RENDER_CONFIG = RenderConfig.isolated()
for _level in range(1, 7):
    setattr(_RENDER_CONFIG.markdown_symbol, f"heading_level_{_level}", "")
_RENDER_CONFIG.markdown_symbol.link = ""
_RENDER_CONFIG.markdown_symbol.unordered_list_item = "-"


def _valid_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not any(ord(char) < 32 or char.isspace() for char in url)
        )
    except ValueError:
        return False


def _validated_entities(text: str, entities: list[ConvertedEntity]) -> list[ConvertedEntity]:
    encoded = text.encode("utf-16-le")
    result = []
    for entity in entities:
        if entity.type not in _ALLOWED_ENTITIES:
            continue
        validated = MessageEntity.model_validate(entity.to_dict())
        if validated.type == "text_link" and not _valid_url(validated.url):
            continue
        start = validated.offset * 2
        end = (validated.offset + validated.length) * 2
        if start < 0 or validated.length <= 0 or end > len(encoded):
            raise ValueError("Entity outside message text")
        encoded[:start].decode("utf-16-le")
        encoded[start:end].decode("utf-16-le")
        result.append(entity)
    code_ranges = [
        (entity.offset, entity.offset + entity.length)
        for entity in result
        if entity.type in {"code", "pre"}
    ]
    return [
        entity
        for entity in result
        if entity.type in {"code", "pre"}
        or not any(
            entity.offset < end and entity.offset + entity.length > start
            for start, end in code_ranges
        )
    ]


def _split_messages(
    text: str, entities: list[ConvertedEntity]
) -> list[tuple[str, list[MessageEntity]]]:
    entities = _validated_entities(text, entities)
    parts = split_entities(text, entities, max_utf16_len=TELEGRAM_MAX_MESSAGE_LENGTH)
    result = []
    for chunk, chunk_entities in parts:
        if len(chunk.encode("utf-16-le")) // 2 > TELEGRAM_MAX_MESSAGE_LENGTH:
            raise ValueError("Message exceeds Telegram limit")
        if not chunk.strip():
            continue
        validated = _validated_entities(chunk, chunk_entities)
        result.append(
            (chunk, [MessageEntity.model_validate(entity.to_dict()) for entity in validated])
        )
    return result


def _is_entity_error(error: TelegramBadRequest) -> bool:
    detail = error.message.casefold()
    return any(
        marker in detail
        for marker in (
            "can't parse entities",
            "cannot parse entities",
            "entity beginning",
            "entity end",
            "entity offset",
            "entity length",
        )
    )


async def send_formatted_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    as_reply: bool = False,
) -> Message | None:
    if not text.strip():
        return None
    try:
        visible, entities = convert(text, latex_escape=False, config=_RENDER_CONFIG)
        chunks = _split_messages(visible, entities)
    except Exception:
        logger.warning("telegram_conversion_error: plain_text_fallback")
        chunks = _split_messages(text, [])
    result = None
    for index, (chunk, entities) in enumerate(chunks):
        send_fn = message.reply if as_reply and index == 0 else message.answer
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            result = await send_fn(chunk, entities=entities, parse_mode=None, reply_markup=markup)
        except TelegramBadRequest as exc:
            if not entities or not _is_entity_error(exc):
                raise
            logger.warning("telegram_entity_error: plain_text_fallback")
            fallback = _split_messages(chunk, [])
            for fallback_index, (plain, _) in enumerate(fallback):
                plain_fn = (
                    message.reply
                    if as_reply and index == 0 and fallback_index == 0
                    else message.answer
                )
                plain_markup = markup if fallback_index == len(fallback) - 1 else None
                result = await plain_fn(
                    plain, entities=[], parse_mode=None, reply_markup=plain_markup
                )
    return result
