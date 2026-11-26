from __future__ import annotations

import asyncio
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app.core.schemas.knowledge import (
    MAX_WEB_DATE_CHARS,
    MAX_WEB_RESULTS,
    MAX_WEB_SNIPPET_CHARS,
    MAX_WEB_TITLE_CHARS,
    KnowledgeStatus,
    WebSearchArguments,
    WebSearchItem,
    WebSearchResult,
)
from app.core.schemas.tools import ToolFailure
from mcp_servers.student_tools.config import (
    DEFAULT_WEB_SEARCH_ENDPOINT,
    DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS,
)


class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> list[WebSearchItem]: ...


class DisabledWebSearchProvider:
    name = "disabled"

    async def search(self, query: str, limit: int) -> list[WebSearchItem]:
        raise ToolFailure("web_search_disabled", "Веб-поиск не настроен.")


class BraveWebSearchProvider:
    name = "brave"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        endpoint: str = DEFAULT_WEB_SEARCH_ENDPOINT,
        timeout_seconds: float = DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Brave Search API key is required")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("timeout must be between 1 and 30 seconds")
        _validate_endpoint(endpoint)

        self._client = client
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout_seconds

    async def search(self, query: str, limit: int) -> list[WebSearchItem]:
        if not 1 <= limit <= MAX_WEB_RESULTS:
            raise ValueError("limit is outside the supported range")

        payload = await self._request(query, limit)
        return _parse_payload(payload, limit)

    async def _request(self, query: str, limit: int) -> object:
        headers = {
            "Accept": "application/json",
            "User-Agent": "itmo-ai-in-sd-bot/0.1",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "count": limit,
            "safesearch": "moderate",
            "text_decorations": "false",
        }

        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(
                    self._endpoint,
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                )
                response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException, httpx.HTTPError):
            raise ToolFailure(
                "web_search_unavailable",
                "Веб-поиск сейчас недоступен. Попробуйте позже.",
            ) from None

        try:
            return response.json()
        except ValueError:
            raise ToolFailure(
                "web_search_unavailable",
                "Поисковый сервис вернул некорректные данные.",
            ) from None


class WebSearchService:
    def __init__(
        self,
        provider: WebSearchProvider,
        *,
        max_results: int = MAX_WEB_RESULTS,
    ) -> None:
        if not 1 <= max_results <= MAX_WEB_RESULTS:
            raise ValueError("max_results is outside the supported range")

        self._provider = provider
        self._max_results = max_results

    async def search(self, arguments: WebSearchArguments) -> WebSearchResult:
        limit = min(arguments.limit, self._max_results)

        try:
            items = await self._provider.search(arguments.query, limit)
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure(
                "web_search_unavailable",
                "Веб-поиск сейчас недоступен. Попробуйте позже.",
            ) from None

        results = items[:limit]
        return WebSearchResult(
            status=KnowledgeStatus.OK if results else KnowledgeStatus.EMPTY,
            query=arguments.query,
            provider=self._provider.name,
            results=results,
            total=len(items),
            truncated=len(items) > len(results),
        )


def _validate_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint must be a valid HTTPS address") from None

    if (
        parsed.scheme != "https"
        or not hostname
        or port is not None
        and not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be a valid HTTPS address")


def _parse_payload(payload: object, limit: int) -> list[WebSearchItem]:
    if not isinstance(payload, dict):
        raise ToolFailure(
            "web_search_unavailable",
            "Поисковый сервис вернул некорректные данные.",
        )

    web = payload.get("web")
    if web is None:
        return []
    if not isinstance(web, dict):
        raise ToolFailure(
            "web_search_unavailable",
            "Поисковый сервис вернул некорректные данные.",
        )

    raw_results = web.get("results")
    if raw_results is None:
        return []
    if not isinstance(raw_results, list):
        raise ToolFailure(
            "web_search_unavailable",
            "Поисковый сервис вернул некорректные данные.",
        )

    items: list[WebSearchItem] = []
    seen_urls: set[str] = set()

    for raw_result in raw_results:
        if len(items) >= limit:
            break

        item = _parse_item(raw_result)
        if item is None or item.url in seen_urls:
            continue

        seen_urls.add(item.url)
        items.append(item)

    return items


def _parse_item(value: object) -> WebSearchItem | None:
    if not isinstance(value, dict):
        return None

    title = clean_web_text(value.get("title"), MAX_WEB_TITLE_CHARS)
    url = clean_web_url(value.get("url"))
    snippet = clean_web_text(value.get("description"), MAX_WEB_SNIPPET_CHARS)
    if title is None or url is None or snippet is None:
        return None

    published_at = clean_web_text(value.get("page_age"), MAX_WEB_DATE_CHARS)
    if published_at is None:
        published_at = clean_web_text(value.get("age"), MAX_WEB_DATE_CHARS)

    return WebSearchItem(
        title=title,
        url=url,
        snippet=snippet,
        published_at=published_at,
    )


def clean_web_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = " ".join(
        "".join(
            " " if ord(character) < 32 or ord(character) == 127 else character
            for character in value
        ).split()
    )
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned

    return cleaned[: limit - 1].rstrip() + "…"


def clean_web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or port is not None
        and not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None

    return candidate
