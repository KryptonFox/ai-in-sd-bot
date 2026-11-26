from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp import types

from app.core.schemas.knowledge import (
    MAX_WEB_DATE_CHARS,
    MAX_WEB_QUERY_CHARS,
    MAX_WEB_RESULTS,
    MAX_WEB_SNIPPET_CHARS,
    MAX_WEB_TITLE_CHARS,
    WebSearchItem,
)
from app.core.schemas.tools import ToolFailure
from mcp_servers.student_tools.config import (
    DEFAULT_EXA_WEB_SEARCH_ENDPOINT,
    DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS,
    normalize_exa_web_search_endpoint,
)
from mcp_servers.student_tools.web_search import clean_web_text, clean_web_url

logger = logging.getLogger("mcp_servers.student_tools.exa_web_search")

EXA_TOOL_NAME = "web_search_exa"
EXA_EMPTY_MESSAGE = "No search results found. Please try a different query."
_EXA_ERROR_MESSAGE = "Поисковый сервис вернул некорректные данные."
_EXA_UNAVAILABLE_MESSAGE = "Веб-поиск сейчас недоступен. Попробуйте позже."


class ExaWebSearchProvider:
    name = "exa"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_EXA_WEB_SEARCH_ENDPOINT,
        timeout_seconds: float = DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS,
        client_factory: Callable[[], Client] | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")

        self._endpoint = normalize_exa_web_search_endpoint(endpoint)
        self._timeout = timeout_seconds
        self._client_factory = client_factory

    async def search(self, query: str, limit: int) -> list[WebSearchItem]:
        if not 1 <= limit <= MAX_WEB_RESULTS:
            raise ValueError("limit is outside the supported range")

        arguments: dict[str, object] = {
            "query": _shorten_query(query),
            "numResults": limit,
        }

        try:
            async with asyncio.timeout(self._timeout):
                async with self._client() as client:
                    result = await client.call_tool_mcp(EXA_TOOL_NAME, arguments)
        except TimeoutError:
            logger.warning("exa_web_search_failed reason=timeout")
            raise ToolFailure("web_search_unavailable", _EXA_UNAVAILABLE_MESSAGE) from None
        except ToolFailure:
            raise
        except Exception as error:
            logger.warning(
                "exa_web_search_failed reason=request_failed error_type=%s",
                type(error).__name__,
            )
            raise ToolFailure("web_search_unavailable", _EXA_UNAVAILABLE_MESSAGE) from None

        return parse_exa_result(result, limit)

    def _client(self) -> Client:
        if self._client_factory is not None:
            return self._client_factory()

        return create_exa_client(self._endpoint, self._timeout)


def create_exa_client(endpoint: str, timeout_seconds: float) -> Client:
    transport = StreamableHttpTransport(endpoint)
    return Client(
        transport,
        name="student-tools-exa-search",
        timeout=timeout_seconds,
        init_timeout=timeout_seconds,
    )


def parse_exa_result(result: types.CallToolResult, limit: int) -> list[WebSearchItem]:
    """Parse Exa's verified text envelope; hosted structuredContent is empty."""
    if result.is_error:
        reason = "rate_limited" if _is_rate_limited(result) else "tool_error"
        logger.warning("exa_web_search_failed reason=%s", reason)
        raise ToolFailure("web_search_unavailable", _EXA_UNAVAILABLE_MESSAGE)

    text_blocks = [
        content.text for content in result.content if isinstance(content, types.TextContent)
    ]
    if not text_blocks:
        logger.warning("exa_web_search_failed reason=malformed_response")
        raise ToolFailure("web_search_unavailable", _EXA_ERROR_MESSAGE)

    items: list[WebSearchItem] = []
    seen_urls: set[str] = set()
    parsed_blocks = 0

    for text in text_blocks:
        if _is_empty_message(text):
            parsed_blocks += 1
            continue
        if not text.strip():
            continue

        records = _find_records(text)
        if not records:
            logger.warning("exa_web_search_failed reason=malformed_response")
            raise ToolFailure("web_search_unavailable", _EXA_ERROR_MESSAGE)

        parsed_blocks += 1
        for record in records:
            item = _parse_record(record)
            if item is None or item.url in seen_urls:
                continue

            seen_urls.add(item.url)
            items.append(item)
            if len(items) >= limit:
                return items

    if parsed_blocks == 0:
        logger.warning("exa_web_search_failed reason=malformed_response")
        raise ToolFailure("web_search_unavailable", _EXA_ERROR_MESSAGE)

    return items


def _shorten_query(query: str) -> str:
    normalized = " ".join(query.split())
    if len(normalized) <= MAX_WEB_QUERY_CHARS:
        return normalized

    return normalized[:MAX_WEB_QUERY_CHARS].rstrip()


def _is_empty_message(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != EXA_EMPTY_MESSAGE:
        return False

    return all(line.startswith("Note: ") for line in lines[:-1])


def _is_rate_limited(result: types.CallToolResult) -> bool:
    meta = result.meta
    return isinstance(meta, dict) and meta.get("ai.exa/rateLimited") is True


def _find_records(text: str) -> list[list[str]]:
    """Find records using the hosted Exa blank/---/blank separator."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first_start = _first_header_start(lines)
    if first_start is None:
        return []

    starts = [first_start]
    for index in range(first_start + 1, len(lines) - 3):
        if _has_headers(lines, index) and _has_record_separator(lines, index):
            starts.append(index)

    records: list[list[str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        records.append(lines[start:end])

    return records


def _first_header_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 3):
        if _has_headers(lines, index):
            return index

    return None


def _has_headers(lines: list[str], index: int) -> bool:
    return (
        _line_value(lines[index], "Title:") is not None
        and _line_value(lines[index + 1], "URL:") is not None
        and _line_value(lines[index + 2], "Published:") is not None
        and _line_value(lines[index + 3], "Author:") is not None
    )


def _has_record_separator(lines: list[str], index: int) -> bool:
    if index < 3:
        return False

    return lines[index - 3] == "" and lines[index - 2] == "---" and lines[index - 1] == ""


def _line_value(line: str, prefix: str) -> str | None:
    if not line.startswith(prefix):
        return None

    return line[len(prefix) :].strip()


def _parse_record(lines: list[str]) -> WebSearchItem | None:
    if len(lines) < 4:
        return None

    title = clean_web_text(_line_value(lines[0], "Title:"), MAX_WEB_TITLE_CHARS)
    url = clean_web_url(_line_value(lines[1], "URL:"))
    published_at = _clean_published_at(_line_value(lines[2], "Published:"))
    snippet = clean_web_text(_record_body(lines[4:]), MAX_WEB_SNIPPET_CHARS)

    if title is None or title.casefold() == "n/a":
        return None
    if url is None or snippet is None:
        return None

    return WebSearchItem(
        title=title,
        url=url,
        snippet=snippet,
        published_at=published_at,
    )


def _record_body(lines: list[str]) -> str | None:
    body_lines = list(lines)

    while body_lines and (not body_lines[-1].strip() or body_lines[-1].strip() == "---"):
        body_lines.pop()

    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)

    if not body_lines:
        return None

    first_line = body_lines[0].strip()
    if first_line.startswith("Highlights:"):
        body_lines[0] = first_line[len("Highlights:") :].strip()
    elif first_line.startswith("Text:"):
        body_lines[0] = first_line[len("Text:") :].strip()
    else:
        body_lines[0] = first_line

    return "\n".join(body_lines)


def _clean_published_at(value: str | None) -> str | None:
    cleaned = clean_web_text(value, MAX_WEB_DATE_CHARS)
    if cleaned is None or cleaned.casefold() == "n/a":
        return None

    return cleaned
