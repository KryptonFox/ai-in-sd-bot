from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.core.schemas.tools import ToolDTO

MAX_MATERIAL_QUERY_CHARS = 300
MAX_MATERIAL_SUBJECT_ID_CHARS = 64
MAX_MATERIAL_RESULTS = 10
DEFAULT_MATERIAL_RESULTS = 5
MAX_MATERIAL_SNIPPET_CHARS = 600
MAX_WEB_QUERY_CHARS = 300
MAX_WEB_RESULTS = 8
DEFAULT_WEB_RESULTS = 5
MAX_WEB_SNIPPET_CHARS = 400
MAX_WEB_TITLE_CHARS = 200
MAX_WEB_DATE_CHARS = 64


class KnowledgeStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"


MaterialQuery = Annotated[
    str,
    Field(
        min_length=2,
        max_length=MAX_MATERIAL_QUERY_CHARS,
        pattern=r"\S",
        description="Слова запроса по утверждённому корпусу учебных материалов.",
    ),
]
MaterialSubjectID = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_MATERIAL_SUBJECT_ID_CHARS,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Необязательный идентификатор предмета из materials://catalog.",
    ),
]
MaterialLimit = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_MATERIAL_RESULTS,
        description="Максимальное число фрагментов; по умолчанию 5.",
    ),
]
WebSearchQuery = Annotated[
    str,
    Field(
        min_length=2,
        max_length=MAX_WEB_QUERY_CHARS,
        pattern=r"\S",
        description="Короткий поисковый запрос по теме.",
    ),
]
WebSearchLimit = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_WEB_RESULTS,
        description="Максимальное число результатов; по умолчанию 5.",
    ),
]


class MaterialSearchArguments(ToolDTO):
    query: MaterialQuery
    subject_id: MaterialSubjectID | None = None
    limit: MaterialLimit = DEFAULT_MATERIAL_RESULTS

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("subject_id", mode="before")
    @classmethod
    def blank_subject_is_absent(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class MaterialSnippet(ToolDTO):
    source_type: Literal["material"] = "material"
    source_id: str
    section_id: str
    citation: str
    source_title: str
    section_title: str
    subject_id: str
    subject_title: str
    lecture_id: str
    lecture_title: str
    text: str = Field(max_length=MAX_MATERIAL_SNIPPET_CHARS)
    trusted: Literal[True] = True


class MaterialSearchResult(ToolDTO):
    status: KnowledgeStatus
    query: str
    subject_id: str | None = None
    results: list[MaterialSnippet] = Field(default_factory=list)
    total: int = Field(ge=0)
    truncated: bool = False


class MaterialCatalogSection(ToolDTO):
    section_id: str
    title: str
    citation: str


class MaterialCatalogDocument(ToolDTO):
    source_id: str
    title: str
    subject_id: str
    subject_title: str
    lecture_id: str
    lecture_title: str
    summary: str
    sections: list[MaterialCatalogSection] = Field(default_factory=list)


class MaterialCatalog(ToolDTO):
    documents: list[MaterialCatalogDocument] = Field(default_factory=list)


class WebSearchArguments(ToolDTO):
    query: WebSearchQuery
    limit: WebSearchLimit = DEFAULT_WEB_RESULTS

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class WebSearchItem(ToolDTO):
    source_type: Literal["web"] = "web"
    title: str = Field(min_length=1, max_length=MAX_WEB_TITLE_CHARS)
    url: str
    snippet: str = Field(min_length=1, max_length=MAX_WEB_SNIPPET_CHARS)
    published_at: str | None = Field(default=None, max_length=MAX_WEB_DATE_CHARS)
    trusted: Literal[False] = False

    @field_validator("url")
    @classmethod
    def require_public_http_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("url must be a valid HTTP address") from None

        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or port is not None
            and not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("url must be a valid HTTP address without credentials")
        return value


class WebSearchResult(ToolDTO):
    status: KnowledgeStatus
    query: str
    provider: str
    results: list[WebSearchItem] = Field(default_factory=list)
    total: int = Field(ge=0)
    truncated: bool = False
