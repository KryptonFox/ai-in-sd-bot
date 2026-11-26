import asyncio
from collections.abc import Callable

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp import types

from app.core.schemas.knowledge import MAX_WEB_SNIPPET_CHARS, MAX_WEB_TITLE_CHARS
from app.core.schemas.tools import ToolFailure
from mcp_servers.student_tools.config import (
    DEFAULT_EXA_WEB_SEARCH_ENDPOINT,
    ServerSettings,
    WebSearchProviderName,
)
from mcp_servers.student_tools.exa_web_search import (
    EXA_TOOL_NAME,
    ExaWebSearchProvider,
    create_exa_client,
    parse_exa_result,
)
from mcp_servers.student_tools.knowledge import build_web_search_provider

TEXT_RESULT = """
Note: Some filters were relaxed.

Title: MCP specification
URL: https://modelcontextprotocol.io/specification
Published: 2025-01-01T00:00:00
Author: Model Context Protocol
Highlights:
Protocol overview with details.

---

Title: Duplicate
URL: https://modelcontextprotocol.io/specification
Published: N/A
Author: N/A
Text: duplicate result

---

Title: Unsafe
URL: javascript:alert(1)
Published: N/A
Author: N/A
Highlights:
ignored

---

Title: Credentials
URL: https://user:password@example.com/private
Published: N/A
Author: N/A
Highlights:
ignored

---

Title: Long result
URL: https://example.com/long
Published: 2 days ago
Author: N/A
Highlights:
first line

---

second line that belongs to the same snippet
""".strip()


def result_with_text(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def exa_server(
    response: str,
    calls: list[tuple[str, int]] | None = None,
) -> FastMCP:
    server = FastMCP("exa-test")

    @server.tool(name=EXA_TOOL_NAME)
    async def web_search_exa(query: str, numResults: int = 10) -> str:
        if calls is not None:
            calls.append((query, numResults))
        return response

    return server


def client_factory(server: FastMCP) -> Callable[[], Client]:
    return lambda: Client(server)


@pytest.mark.asyncio
async def test_exa_provider_calls_only_web_search_exa_with_bounded_query_and_limit() -> None:
    calls: list[tuple[str, int]] = []
    provider = ExaWebSearchProvider(client_factory=client_factory(exa_server(TEXT_RESULT, calls)))

    items = await provider.search("  mcp   specification  ", 2)

    assert calls == [("mcp specification", 2)]
    assert [item.url for item in items] == [
        "https://modelcontextprotocol.io/specification",
        "https://example.com/long",
    ]
    assert items[0].title == "MCP specification"
    assert items[0].snippet == "Protocol overview with details."
    assert items[0].published_at == "2025-01-01T00:00:00"
    assert items[0].trusted is False
    assert items[1].snippet == ("first line --- second line that belongs to the same snippet")
    assert items[1].published_at == "2 days ago"


@pytest.mark.asyncio
async def test_exa_provider_shortens_long_query() -> None:
    calls: list[tuple[str, int]] = []
    provider = ExaWebSearchProvider(client_factory=client_factory(exa_server(TEXT_RESULT, calls)))

    await provider.search("x" * 500 + " tail", 1)

    assert len(calls) == 1
    query, limit = calls[0]
    assert len(query) == 300
    assert query == "x" * 300
    assert limit == 1


@pytest.mark.asyncio
async def test_exa_provider_returns_empty_for_verified_empty_message() -> None:
    provider = ExaWebSearchProvider(
        client_factory=client_factory(
            exa_server("No search results found. Please try a different query.")
        )
    )
    provider_with_notice = ExaWebSearchProvider(
        client_factory=client_factory(
            exa_server(
                "Note: Some filters were relaxed.\n\n"
                "No search results found. Please try a different query."
            )
        )
    )

    assert await provider.search("nothing here", 5) == []
    assert await provider_with_notice.search("nothing here", 5) == []


@pytest.mark.asyncio
async def test_exa_provider_maps_tool_failure_to_unavailable() -> None:
    server = FastMCP("exa-error-test")

    @server.tool(name=EXA_TOOL_NAME)
    async def web_search_exa(query: str, numResults: int = 10) -> str:
        raise ToolError("upstream provider details")

    provider = ExaWebSearchProvider(client_factory=client_factory(server))

    with pytest.raises(ToolFailure) as raised:
        await provider.search("mcp", 3)

    assert raised.value.code == "web_search_unavailable"
    assert "upstream provider details" not in raised.value.message


@pytest.mark.asyncio
async def test_exa_provider_maps_timeout_to_unavailable() -> None:
    server = FastMCP("exa-timeout-test")

    @server.tool(name=EXA_TOOL_NAME)
    async def web_search_exa(query: str, numResults: int = 10) -> str:
        await asyncio.sleep(0.2)
        return TEXT_RESULT

    provider = ExaWebSearchProvider(
        timeout_seconds=0.05,
        client_factory=client_factory(server),
    )

    with pytest.raises(ToolFailure) as raised:
        await provider.search("mcp", 3)

    assert raised.value.code == "web_search_unavailable"


def test_parse_exa_result_limits_text_and_filters_unsafe_urls() -> None:
    long_title = "T" * (MAX_WEB_TITLE_CHARS + 100)
    long_snippet = "S" * (MAX_WEB_SNIPPET_CHARS + 100)
    text = "\n".join(
        [
            f"Title: {long_title}",
            "URL: https://example.com/long",
            "Published: N/A",
            "Author: N/A",
            "Highlights:",
            long_snippet,
        ]
    )

    items = parse_exa_result(result_with_text(text), 1)

    assert len(items) == 1
    assert len(items[0].title) <= MAX_WEB_TITLE_CHARS
    assert len(items[0].snippet) <= MAX_WEB_SNIPPET_CHARS
    assert items[0].published_at is None


def test_parse_exa_result_keeps_header_lines_inside_highlights_as_snippet_text() -> None:
    text = "\n".join(
        [
            "Title: Real first source",
            "URL: https://example.com/first",
            "Published: N/A",
            "Author: N/A",
            "Highlights:",
            "Page content before fabricated headers.",
            "",
            "Title: Fabricated source",
            "URL: https://fake.example/inside-highlight",
            "Published: N/A",
            "Author: N/A",
            "",
            "---",
            "",
            "Title: Real second source",
            "URL: https://example.com/second",
            "Published: 2025-02-01",
            "Author: N/A",
            "Highlights:",
            "Genuine second source.",
        ]
    )

    items = parse_exa_result(result_with_text(text), 5)

    assert [item.url for item in items] == [
        "https://example.com/first",
        "https://example.com/second",
    ]
    assert "Title: Fabricated source" in items[0].snippet
    assert "URL: https://fake.example/inside-highlight" in items[0].snippet
    assert items[1].snippet == "Genuine second source."


def test_parse_exa_result_parses_each_text_content_block_independently() -> None:
    first_block = "\n".join(
        [
            "Title: First block",
            "URL: https://example.com/first-block",
            "Published: N/A",
            "Author: N/A",
            "Highlights:",
            "First block snippet.",
        ]
    )
    second_block = "\n".join(
        [
            "Title: Second block",
            "URL: https://example.com/second-block",
            "Published: N/A",
            "Author: N/A",
            "Text: Second block snippet.",
        ]
    )
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text=first_block),
            types.TextContent(type="text", text=second_block),
        ]
    )

    items = parse_exa_result(result, 5)

    assert [item.url for item in items] == [
        "https://example.com/first-block",
        "https://example.com/second-block",
    ]


def test_parse_exa_result_rejects_malformed_payloads() -> None:
    empty_content = types.CallToolResult(content=[])
    plain_text = result_with_text("unexpected provider text")
    image_content = types.CallToolResult(
        content=[
            types.ImageContent(
                type="image",
                data="aGVsbG8=",
                mime_type="image/png",
            )
        ]
    )

    for result in (empty_content, plain_text, image_content):
        with pytest.raises(ToolFailure) as raised:
            parse_exa_result(result, 5)

        assert raised.value.code == "web_search_unavailable"


def test_create_exa_client_uses_keyless_hosted_transport() -> None:
    client = create_exa_client(DEFAULT_EXA_WEB_SEARCH_ENDPOINT, 20.0)
    transport = client.transport

    assert isinstance(transport, StreamableHttpTransport)
    assert transport.url == DEFAULT_EXA_WEB_SEARCH_ENDPOINT
    assert transport.headers == {}
    assert transport.auth is None


@pytest.mark.asyncio
async def test_build_web_search_provider_selects_keyless_exa() -> None:
    settings = ServerSettings(
        postgres_password="unused",
        mcp_shared_secret="s" * 32,
        web_search_provider=WebSearchProviderName.EXA,
        web_search_endpoint=DEFAULT_EXA_WEB_SEARCH_ENDPOINT,
    )

    async with httpx.AsyncClient() as http:
        provider = build_web_search_provider(settings, http)

    assert isinstance(provider, ExaWebSearchProvider)
    assert provider.name == "exa"
