from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.core.formatters import render_markdown, send_formatted_message


def test_render_markdown_empty():
    assert render_markdown("") == ""
    assert render_markdown(None) == ""


def test_render_markdown_bold_and_italic():
    text = "Это **жирный** и *курсив*, а это _тоже курсив_."

    rendered = render_markdown(text)

    assert "<b>жирный</b>" in rendered
    assert "<i>курсив</i>" in rendered
    assert "<i>тоже курсив</i>" in rendered


def test_render_markdown_preserves_snake_case():
    text = "Переменная POSTGRES_PASSWORD_KEY и my_func_name не должны ломаться."

    rendered = render_markdown(text)

    assert "<i>" not in rendered
    assert "POSTGRES_PASSWORD_KEY" in rendered
    assert "my_func_name" in rendered


def test_render_markdown_inline_code():
    text = "Используйте `docker compose up -d` и `x < 5 & y > 2`."

    rendered = render_markdown(text)

    assert "<code>docker compose up -d</code>" in rendered
    assert "<code>x &lt; 5 &amp; y &gt; 2</code>" in rendered


def test_render_markdown_code_block():
    text = "```python\ndef add(a, b):\n    return a < b & True\n```"

    rendered = render_markdown(text)

    assert '<pre><code class="language-python">' in rendered
    assert "return a &lt; b &amp; True" in rendered
    assert "</code></pre>" in rendered


def test_render_markdown_headers_and_quotes():
    text = "### Заголовок темы\n> Важная цитата или совет"

    rendered = render_markdown(text)

    assert "<b>Заголовок темы</b>" in rendered
    assert "<blockquote>Важная цитата или совет</blockquote>" in rendered


def test_render_markdown_links():
    text = "Документация: [ITMO](https://itmo.ru)"

    rendered = render_markdown(text)

    assert '<a href="https://itmo.ru">ITMO</a>' in rendered


def test_render_markdown_escapes_raw_html():
    text = "Тег <script>alert(1)</script> и амперсанд &"

    rendered = render_markdown(text)

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&amp;" in rendered


@pytest.mark.asyncio
async def test_send_formatted_message_success():
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    await send_formatted_message(message, "**Успех**")

    message.answer.assert_called_once()
    called_text = message.answer.call_args.args[0]
    called_parse_mode = message.answer.call_args.kwargs.get("parse_mode")

    assert called_text == "<b>Успех</b>"
    assert called_parse_mode == ParseMode.HTML


@pytest.mark.asyncio
async def test_send_formatted_message_fallback_on_telegram_error():
    message = MagicMock(spec=Message)
    error = TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities")
    message.answer = AsyncMock(side_effect=[error, MagicMock()])
    raw_text = "Текст с битой разметкой"

    await send_formatted_message(message, raw_text)

    assert message.answer.call_count == 2
    fallback_call = message.answer.call_args_list[1]
    assert fallback_call.args[0] == raw_text
    assert fallback_call.kwargs.get("parse_mode") is None
