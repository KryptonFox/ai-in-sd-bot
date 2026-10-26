from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Message, MessageEntity
from telegramify_markdown.config import get_runtime_config
from telegramify_markdown.entity import MessageEntity as ConvertedEntity

from app.core import formatters
from app.core.formatters import send_formatted_message


@pytest.fixture
def message():
    return MagicMock(spec=Message, answer=AsyncMock(), reply=AsyncMock())


def entity_text(text, entity):
    encoded = text.encode("utf-16-le")
    return encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def assert_valid_delivery(calls):
    for call in calls:
        text = call.args[0]
        assert text.strip()
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        assert call.kwargs["parse_mode"] is None
        encoded = text.encode("utf-16-le")
        for entity in call.kwargs["entities"]:
            assert isinstance(entity, MessageEntity)
            assert entity.type in {"bold", "italic", "code", "pre", "text_link"}
            assert entity.offset >= 0 and entity.length > 0
            assert (entity.offset + entity.length) * 2 <= len(encoded)
            encoded[: entity.offset * 2].decode("utf-16-le")
            assert entity_text(text, entity)


async def test_text_and_entities_are_sent_without_parse_mode(message):
    await send_formatted_message(message, "Это **жирный** и *курсив*, а это _тоже курсив_.")
    call = message.answer.call_args
    assert call.args[0] == "Это жирный и курсив, а это тоже курсив."
    assert [
        (entity.type, entity_text(call.args[0], entity)) for entity in call.kwargs["entities"]
    ] == [
        ("bold", "жирный"),
        ("italic", "курсив"),
        ("italic", "тоже курсив"),
    ]
    assert_valid_delivery([call])


async def test_snake_case_remains_literal(message):
    text = "POSTGRES_PASSWORD_KEY и my_func_name"
    await send_formatted_message(message, text)
    assert message.answer.call_args.args[0] == text
    assert message.answer.call_args.kwargs["entities"] == []


async def test_code_keeps_symbols_and_indentation(message):
    code = "def add(a, b):\n    return a < b & True  # <>& **literal** $x$"
    await send_formatted_message(message, "```python\n" + code + "\n```")
    call = message.answer.call_args
    assert call.args[0] == code
    assert len(call.kwargs["entities"]) == 1
    entity = call.kwargs["entities"][0]
    assert entity.type == "pre" and entity.language == "python"
    assert entity_text(call.args[0], entity) == code
    assert_valid_delivery([call])


async def test_inline_code_and_raw_html_are_never_sent_as_html(message):
    await send_formatted_message(message, "`x < 5 & y > 2` и <script>alert(1)</script>")
    call = message.answer.call_args
    code = next(entity for entity in call.kwargs["entities"] if entity.type == "code")
    assert entity_text(call.args[0], code) == "x < 5 & y > 2"
    assert "alert(1)" in call.args[0]
    assert_valid_delivery([call])


@pytest.mark.parametrize(
    "url",
    [
        "https://itmo.ru/path?a=1&b=2",
        'https://example.org/?q="&b=2',
    ],
)
async def test_http_links_with_attributes(message, url):
    await send_formatted_message(message, f"[label](<{url}>)")
    call = message.answer.call_args
    assert call.args[0] == "label"
    link = next(entity for entity in call.kwargs["entities"] if entity.type == "text_link")
    assert link.url == url and entity_text(call.args[0], link) == "label"
    assert_valid_delivery([call])


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file",
        "javascript:alert(1)",
        "tg://user?id=42",
        "https://example.org:99999/file",
        "https:///",
        "http://example.org/bad path",
    ],
)
async def test_disallowed_links_lose_entity_but_keep_label(message, url):
    await send_formatted_message(message, f"[label](<{url}>)")
    call = message.answer.call_args
    assert "label" in call.args[0]
    assert not any(entity.type == "text_link" for entity in call.kwargs["entities"])
    assert_valid_delivery([call])


async def test_headings_lists_and_config_are_isolated(message):
    global_config = get_runtime_config()
    before = vars(global_config.markdown_symbol).copy()
    await send_formatted_message(message, "# Заголовок\n- один\n- два\n\n[сайт](https://itmo.ru)")
    call = message.answer.call_args
    assert call.args[0] == "Заголовок\n- один\n- два\n\nсайт"
    assert any(entity.type == "bold" for entity in call.kwargs["entities"])
    assert not any(entity.type == "underline" for entity in call.kwargs["entities"])
    assert vars(global_config.markdown_symbol) == before
    assert_valid_delivery([call])


async def test_latex_conversion_is_disabled_and_code_keeps_math_literal(message, monkeypatch):
    original = formatters.convert
    calls = []

    def recording_convert(text, **kwargs):
        calls.append(kwargs)
        return original(text, **kwargs)

    monkeypatch.setattr(formatters, "convert", recording_convert)
    source = r"`\(\alpha + \beta\)` и `x^2`"
    await send_formatted_message(message, source)
    call = message.answer.call_args
    assert calls[0]["latex_escape"] is False
    assert r"\alpha + \beta" in call.args[0]
    assert "α" not in call.args[0]
    assert_valid_delivery([call])


@pytest.mark.parametrize("text", ["**open", "`unclosed", "[broken](", "```cpp\nx < 5 & y > 2"])
async def test_damaged_markdown_is_sendable(message, text):
    await send_formatted_message(message, text)
    assert message.answer.await_count == 1
    assert_valid_delivery(message.answer.call_args_list)
    if text in {"**open", "`unclosed", "[broken]("}:
        assert message.answer.call_args.args[0] == text


@pytest.mark.parametrize(
    "body",
    [
        "x < 5 && y > 2; // **literal** & <script>\n" * 400 + "done",
        "x" * 12000,
        "👨‍👩‍👧‍👦 е\u0301 🇷🇺 👍🏽 " * 1200 + "done",
    ],
)
async def test_long_code_is_split_with_valid_utf16_entities(message, body):
    await send_formatted_message(message, "```cpp\n" + body + "\n```")
    calls = message.answer.call_args_list
    assert len(calls) > 1
    assert "".join(call.args[0] for call in calls) == body
    assert_valid_delivery(calls)
    for call in calls:
        assert len(call.kwargs["entities"]) == 1
        entity = call.kwargs["entities"][0]
        assert entity.type == "pre" and entity.language == "cpp"
        assert entity_text(call.args[0], entity) == call.args[0]


@pytest.mark.parametrize("count", [4095, 4096, 4097, 8192])
async def test_limit_boundary_with_entities(message, count):
    await send_formatted_message(message, "**" + "&" * count + "**")
    calls = message.answer.call_args_list
    assert len(calls) == (count + 4095) // 4096
    assert "".join(call.args[0] for call in calls) == "&" * count
    assert_valid_delivery(calls)


async def test_offsets_after_non_bmp_emoji_and_combining_characters(message):
    await send_formatted_message(message, "😀👨‍👩‍👧‍👦 е\u0301 **жирный** `код`")
    call = message.answer.call_args
    assert [
        (entity.type, entity_text(call.args[0], entity)) for entity in call.kwargs["entities"]
    ] == [
        ("bold", "жирный"),
        ("code", "код"),
    ]
    assert_valid_delivery([call])


async def test_convert_and_split_are_called_once_for_the_whole_response(message, monkeypatch):
    convert, split = formatters.convert, formatters.split_entities
    converted, split_calls = [], []

    def recording_convert(text, **kwargs):
        converted.append(text)
        return convert(text, **kwargs)

    def recording_split(text, entities, **kwargs):
        split_calls.append(kwargs)
        return split(text, entities, **kwargs)

    monkeypatch.setattr(formatters, "convert", recording_convert)
    monkeypatch.setattr(formatters, "split_entities", recording_split)
    source = "**" + "a" * 9000 + "**"
    await send_formatted_message(message, source)
    assert converted == [source]
    assert split_calls == [{"max_utf16_len": 4096}]


@pytest.mark.parametrize("source", ["", " \n\t", "<b></b>", "```text\n  \n```"])
async def test_empty_output_never_calls_telegram(message, source):
    assert await send_formatted_message(message, source) is None
    message.answer.assert_not_awaited()
    message.reply.assert_not_awaited()


async def test_conversion_failure_sends_original_text_split_by_library(message, monkeypatch):
    def broken_convert(text, **kwargs):
        raise RuntimeError("PRIVATE RESPONSE")

    monkeypatch.setattr(formatters, "convert", broken_convert)
    source = "**" + "😀" * 3000 + "**"
    await send_formatted_message(message, source)
    calls = message.answer.call_args_list
    assert len(calls) == 2
    assert "".join(call.args[0] for call in calls) == source
    assert all(call.kwargs["entities"] == [] for call in calls)
    assert_valid_delivery(calls)


@pytest.mark.parametrize("offset,length", [(1, 1), (0, 1), (-1, 2), (0, 100), (0, 0)])
async def test_invalid_entity_ranges_fall_back_to_source(message, monkeypatch, offset, length):
    monkeypatch.setattr(
        formatters,
        "convert",
        lambda *args, **kwargs: (
            "😀x",
            [ConvertedEntity(type="bold", offset=offset, length=length)],
        ),
    )
    await send_formatted_message(message, "**original**")
    call = message.answer.call_args
    assert call.args[0] == "**original**" and call.kwargs["entities"] == []


async def test_nested_style_is_removed_from_code_ranges(message):
    await send_formatted_message(message, "**nested `x < y` text**")
    call = message.answer.call_args
    assert call.args[0] == "nested x < y text"
    assert [entity.type for entity in call.kwargs["entities"]] == ["code"]
    assert_valid_delivery([call])


async def test_unknown_entities_are_removed_without_changing_text(message, monkeypatch):
    monkeypatch.setattr(
        formatters,
        "convert",
        lambda *args, **kwargs: (
            "visible",
            [ConvertedEntity(type="custom_emoji", offset=0, length=7)],
        ),
    )
    await send_formatted_message(message, "source")
    assert message.answer.call_args.args[0] == "visible"
    assert message.answer.call_args.kwargs["entities"] == []


async def test_entity_failure_resends_only_rejected_visible_part(message):
    error = TelegramBadRequest(method=MagicMock(), message="can't parse entities")
    message.answer.side_effect = [MagicMock(), error, MagicMock(), MagicMock()]
    await send_formatted_message(message, "**" + "a" * 9000 + "**")
    calls = message.answer.call_args_list
    assert len(calls) == 4
    assert [len(call.args[0]) for call in calls] == [4096, 4096, 4096, 808]
    assert calls[0].kwargs["entities"]
    assert calls[1].kwargs["entities"]
    assert calls[2].kwargs["entities"] == []
    assert calls[3].kwargs["entities"]
    assert_valid_delivery(calls)


async def test_reply_first_keyboard_last_also_on_fallback(message):
    keyboard = MagicMock()
    error = TelegramBadRequest(method=MagicMock(), message="entity offset is out of range")
    message.reply.side_effect = [error, MagicMock()]
    await send_formatted_message(
        message, "**" + "a" * 9000 + "**", as_reply=True, reply_markup=keyboard
    )
    assert message.reply.await_count == 2
    assert message.reply.call_args_list[0].kwargs["reply_markup"] is None
    assert message.reply.call_args_list[1].kwargs["entities"] == []
    assert message.answer.await_count == 2
    assert message.answer.call_args_list[0].kwargs["reply_markup"] is None
    assert message.answer.call_args_list[1].kwargs["reply_markup"] is keyboard


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=MagicMock(), message="chat not found"),
        TelegramBadRequest(method=MagicMock(), message="message to be replied not found"),
        TelegramBadRequest(method=MagicMock(), message="message is too long"),
        TelegramNetworkError(method=MagicMock(), message="offline"),
    ],
)
async def test_other_delivery_errors_are_not_retried(message, error):
    message.answer.side_effect = [MagicMock(), error]
    with pytest.raises(type(error)):
        await send_formatted_message(message, "**" + "a" * 9000 + "**")
    assert message.answer.await_count == 2


async def test_fallback_delivery_error_is_not_retried_again(message):
    error = TelegramBadRequest(method=MagicMock(), message="can't parse entities")
    message.answer.side_effect = [
        error,
        TelegramNetworkError(method=MagicMock(), message="offline"),
    ]
    with pytest.raises(TelegramNetworkError):
        await send_formatted_message(message, "**a**")
    assert message.answer.await_count == 2


async def test_aiogram_global_parse_mode_is_explicitly_overridden():
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.types import Chat

    bot = Bot(
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    bot.session = AsyncMock()
    message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private")).as_(
        bot
    )
    try:
        await send_formatted_message(message, "**bold** `<>&`")
        method = bot.session.call_args.args[1]
        assert method.parse_mode is None
        assert method.text == "bold <>&"
        assert [entity.type for entity in method.entities] == ["bold", "code"]
    finally:
        await bot.session.close()


async def test_conversion_failure_does_not_request_another_llm_answer(message, monkeypatch):
    from types import SimpleNamespace

    from app.handlers.dialog import _execute_dialog

    def broken_convert(text, **kwargs):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(formatters, "convert", broken_convert)
    service = SimpleNamespace(
        process_user_message=AsyncMock(return_value="**answer**"), queue_manager=None
    )
    from typing import cast

    from app.services.dialog_service import DialogService

    await _execute_dialog(message, cast(DialogService, service), 42, "question")
    service.process_user_message.assert_awaited_once_with(42, "question")
    message.answer.assert_awaited_once()
    assert message.answer.call_args.args[0] == "**answer**"
    assert message.answer.call_args.kwargs["entities"] == []
