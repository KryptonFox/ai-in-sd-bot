import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types

from app.core.schemas.knowledge import (
    MAX_MATERIAL_RESULTS,
    MAX_MATERIAL_SNIPPET_CHARS,
    MaterialSearchArguments,
    WebSearchItem,
)
from app.core.schemas.tools import ToolFailure
from mcp_servers.student_tools.config import ServerSettings
from mcp_servers.student_tools.knowledge import register_knowledge_tools
from mcp_servers.student_tools.materials import MaterialCorpus

SECRET = "knowledge-test-secret-0123456789abcdef"


class RecordingWebProvider:
    name = "recording"

    def __init__(self, items: list[WebSearchItem]) -> None:
        self.items = items
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int) -> list[WebSearchItem]:
        self.calls.append((query, limit))
        return self.items[:limit]


def settings() -> ServerSettings:
    return ServerSettings(
        postgres_password="unused",
        mcp_shared_secret=SECRET,
    )


@asynccontextmanager
async def knowledge_client(
    *,
    provider: RecordingWebProvider | None = None,
) -> AsyncGenerator[Client]:
    server = FastMCP("knowledge-test", strict_input_validation=True, mask_error_details=True)
    async with httpx.AsyncClient() as http:
        register_knowledge_tools(server, settings(), http, web_provider=provider)
        async with Client(server) as client:
            yield client


@pytest.mark.asyncio
async def test_catalog_resource_lists_approved_documents_and_stable_sections() -> None:
    async with knowledge_client() as client:
        resources = await client.list_resources()
        catalog_resources = [
            resource for resource in resources if str(resource.uri) == "materials://catalog"
        ]

        assert len(catalog_resources) == 1
        contents = await client.read_resource("materials://catalog")
        assert isinstance(contents[0], types.TextResourceContents)
        catalog = json.loads(contents[0].text)

    assert len(catalog["documents"]) == 3
    source_ids = [document["source_id"] for document in catalog["documents"]]
    assert source_ids == [
        "mcp-protocol",
        "context-and-structured-outputs",
        "idempotency-and-delivery",
    ]

    citations: list[str] = []
    for document in catalog["documents"]:
        assert document["subject_id"]
        assert document["lecture_id"]
        assert document["sections"]
        assert "materials/" not in json.dumps(document)
        assert ".md" not in json.dumps(document)
        for section in document["sections"]:
            assert (
                section["citation"]
                == f"materials://{document['source_id']}#{section['section_id']}"
            )
            citations.append(section["citation"])

    assert len(citations) == len(set(citations))
    assert "materials://mcp-protocol#mcp-overview" in citations


@pytest.mark.asyncio
async def test_search_materials_returns_bounded_snippets_with_provenance() -> None:
    async with knowledge_client() as client:
        result = await client.call_tool(
            "search_materials",
            {"query": "как не отправить уведомление дважды", "limit": 3},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["status"] == "ok"
    assert 1 <= len(payload["results"]) <= 3

    for snippet in payload["results"]:
        assert snippet["source_type"] == "material"
        assert snippet["trusted"] is True
        assert snippet["citation"] == f"materials://{snippet['source_id']}#{snippet['section_id']}"
        assert len(snippet["text"]) <= MAX_MATERIAL_SNIPPET_CHARS
        assert snippet["subject_id"]
        assert snippet["lecture_id"]

    assert any(snippet["source_id"] == "idempotency-and-delivery" for snippet in payload["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_source"),
    [
        ("контекстное окно модели", "context-and-structured-outputs"),
        ("инструменты и ресурсы MCP", "mcp-protocol"),
        ("сбой доставки и повторная попытка", "idempotency-and-delivery"),
    ],
)
async def test_search_materials_handles_russian_paraphrases(
    query: str,
    expected_source: str,
) -> None:
    async with knowledge_client() as client:
        result = await client.call_tool("search_materials", {"query": query, "limit": 1})

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["results"][0]["source_id"] == expected_source


@pytest.mark.asyncio
async def test_search_materials_returns_empty_status_and_filters_by_subject() -> None:
    async with knowledge_client() as client:
        empty = await client.call_tool(
            "search_materials",
            {"query": "квантовая хромодинамика"},
        )
        filtered = await client.call_tool(
            "search_materials",
            {"query": "отправка уведомления", "subject_id": "delivery", "limit": 4},
        )

    assert empty.is_error is not True
    assert empty.structured_content is not None
    assert empty.structured_content["status"] == "empty"
    assert empty.structured_content["results"] == []
    assert empty.structured_content["total"] == 0

    assert filtered.is_error is not True
    assert filtered.structured_content is not None
    assert filtered.structured_content["results"]
    assert all(
        snippet["subject_id"] == "delivery" for snippet in filtered.structured_content["results"]
    )


@pytest.mark.asyncio
async def test_search_materials_rejects_unknown_subject_and_out_of_range_limit() -> None:
    async with knowledge_client() as client:
        with pytest.raises(ToolError, match="unknown_subject"):
            await client.call_tool(
                "search_materials",
                {"query": "контекст", "subject_id": "unknown-subject"},
            )

        with pytest.raises(ToolError):
            await client.call_tool(
                "search_materials",
                {"query": "контекст", "limit": MAX_MATERIAL_RESULTS + 1},
            )

        with pytest.raises(ToolError):
            await client.call_tool(
                "search_materials",
                {"query": "контекст", "subject_id": "../../etc/passwd"},
            )


@pytest.mark.asyncio
async def test_search_web_requires_trusted_token_and_strict_boolean_permission() -> None:
    item = WebSearchItem(
        title="MCP specification",
        url="https://modelcontextprotocol.io/specification",
        snippet="Protocol description",
    )
    provider = RecordingWebProvider([item])

    async with knowledge_client(provider=provider) as client:
        missing = await client.call_tool(
            "search_web",
            {"query": "mcp specification"},
            raise_on_error=False,
        )
        forged = await client.call_tool(
            "search_web",
            {"query": "mcp specification"},
            meta={"app_token": "wrong-token", "web_search_allowed": True},
            raise_on_error=False,
        )
        denied = await client.call_tool(
            "search_web",
            {"query": "mcp specification"},
            meta={"app_token": SECRET, "web_search_allowed": False},
            raise_on_error=False,
        )
        non_boolean = await client.call_tool(
            "search_web",
            {"query": "mcp specification"},
            meta={"app_token": SECRET, "web_search_allowed": "true"},
            raise_on_error=False,
        )

        assert missing.is_error is True
        assert forged.is_error is True
        assert denied.is_error is True
        assert non_boolean.is_error is True
        assert provider.calls == []

        allowed = await client.call_tool(
            "search_web",
            {"query": "mcp specification", "limit": 3},
            meta={"app_token": SECRET, "web_search_allowed": True},
        )

    assert allowed.is_error is not True
    assert allowed.structured_content is not None
    assert allowed.structured_content["status"] == "ok"
    assert allowed.structured_content["provider"] == "recording"
    assert allowed.structured_content["results"][0]["source_type"] == "web"
    assert allowed.structured_content["results"][0]["trusted"] is False
    assert provider.calls == [("mcp specification", 3)]


@pytest.mark.asyncio
async def test_search_web_empty_provider_result_is_not_an_error() -> None:
    provider = RecordingWebProvider([])

    async with knowledge_client(provider=provider) as client:
        result = await client.call_tool(
            "search_web",
            {"query": "редкая тема"},
            meta={"app_token": SECRET, "web_search_allowed": True},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["status"] == "empty"
    assert result.structured_content["results"] == []


@pytest.mark.asyncio
async def test_search_web_rejects_empty_shared_secret_before_token_comparison() -> None:
    provider = RecordingWebProvider(
        [
            WebSearchItem(
                title="MCP specification",
                url="https://modelcontextprotocol.io/specification",
                snippet="Protocol description",
            )
        ]
    )
    server = FastMCP("knowledge-test", strict_input_validation=True, mask_error_details=True)
    empty_secret = ServerSettings(postgres_password="unused", mcp_shared_secret="")

    async with httpx.AsyncClient() as http:
        register_knowledge_tools(server, empty_secret, http, web_provider=provider)
        async with Client(server) as client:
            result = await client.call_tool(
                "search_web",
                {"query": "mcp specification"},
                meta={"app_token": "", "web_search_allowed": True},
                raise_on_error=False,
            )

    assert result.is_error is True
    assert provider.calls == []


@pytest.mark.asyncio
async def test_registered_knowledge_tools_are_marked_read_only() -> None:
    async with knowledge_client() as client:
        tools = await client.list_tools()

    discovered = {tool.name: tool for tool in tools}
    assert set(discovered) == {"search_materials", "search_web"}
    for tool in discovered.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is not None


def test_material_corpus_rejects_unknown_subject_without_file_access() -> None:
    corpus = MaterialCorpus()

    with pytest.raises(ToolFailure) as raised:
        corpus.search(MaterialSearchArguments(query="контекст", subject_id="unknown"))

    assert raised.value.code == "unknown_subject"

    with pytest.raises(ValueError):
        MaterialSearchArguments(
            query="контекст",
            subject_id="../context-and-structured-outputs",
        )
