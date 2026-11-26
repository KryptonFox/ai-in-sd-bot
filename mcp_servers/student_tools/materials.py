from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.schemas.knowledge import (
    KnowledgeStatus,
    MaterialCatalog,
    MaterialCatalogDocument,
    MaterialCatalogSection,
    MaterialSearchArguments,
    MaterialSearchResult,
    MaterialSnippet,
)
from app.core.schemas.tools import ToolFailure
from mcp_servers.student_tools.material_search import (
    excerpt,
    query_terms,
    score_section,
    tokenize,
)

MATERIALS_ROOT = Path(__file__).resolve().parent / "data" / "materials"

_SECTION_MARKER = re.compile(r"^<!--\s*section:\s*([a-z0-9][a-z0-9-]{0,63})\s*-->\s*$")
_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$")


@dataclass(frozen=True)
class DocumentSpec:
    source_id: str
    filename: str
    title: str
    subject_id: str
    subject_title: str
    lecture_id: str
    lecture_title: str
    summary: str


@dataclass(frozen=True)
class ParsedSection:
    section_id: str
    title: str
    text: str
    tokens: tuple[str, ...]
    title_tokens: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class ParsedDocument:
    spec: DocumentSpec
    sections: tuple[ParsedSection, ...]


@dataclass(frozen=True)
class _ScoredSection:
    document: ParsedDocument
    section: ParsedSection
    score: float
    matched_terms: tuple[str, ...]


STARTER_DOCUMENTS: tuple[DocumentSpec, ...] = (
    DocumentSpec(
        source_id="mcp-protocol",
        filename="mcp-protocol.md",
        title="MCP: протокол контекста модели",
        subject_id="mcp",
        subject_title="Протоколы интеграции",
        lecture_id="intro-mcp",
        lecture_title="Введение в MCP",
        summary=(
            "Назначение MCP, инструменты, ресурсы, подсказки и границы доверия "
            "между приложением и сервером."
        ),
    ),
    DocumentSpec(
        source_id="context-and-structured-outputs",
        filename="context-and-structured-outputs.md",
        title="Контекст модели и структурированные ответы",
        subject_id="context",
        subject_title="Контекст и контракты",
        lecture_id="intro-context",
        lecture_title="Контекст и структурированные ответы",
        summary=(
            "Подготовка контекстного окна, поиск по корпусу, схемы ответов, "
            "валидация и ссылки на источники."
        ),
    ),
    DocumentSpec(
        source_id="idempotency-and-delivery",
        filename="idempotency-and-delivery.md",
        title="Идемпотентность и доставка уведомлений",
        subject_id="delivery",
        subject_title="Надёжность и доставка",
        lecture_id="intro-delivery",
        lecture_title="Идемпотентность и доставка",
        summary=(
            "Повторные попытки, ключи идемпотентности, очередь исходящих сообщений "
            "и состояния доставки."
        ),
    ),
)


def _parse_markdown(spec: DocumentSpec, root: Path) -> ParsedDocument:
    path = root / spec.filename

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ToolFailure(
            "materials_unavailable",
            "Учебные материалы сейчас недоступны.",
        ) from error

    sections: list[ParsedSection] = []
    seen_ids: set[str] = set()
    pending_id: str | None = None
    current_id: str | None = None
    current_title = ""
    current_lines: list[str] = []

    def finish_section() -> None:
        if current_id is None:
            return

        text = "\n".join(current_lines).strip()
        if not text:
            raise ToolFailure("materials_unavailable", "Раздел учебных материалов пуст.")

        sections.append(
            ParsedSection(
                section_id=current_id,
                title=current_title,
                text=text,
                tokens=tokenize(f"{current_title}\n{text}"),
                title_tokens=tokenize(current_title),
                order=len(sections),
            )
        )

    for line in content.splitlines():
        marker = _SECTION_MARKER.match(line)
        if marker:
            if pending_id is not None:
                raise ToolFailure(
                    "materials_unavailable",
                    "В учебных материалах нарушена разметка разделов.",
                )
            pending_id = marker.group(1)
            continue

        heading = _SECTION_HEADING.match(line)
        if heading:
            if pending_id is None:
                raise ToolFailure(
                    "materials_unavailable",
                    "У раздела учебных материалов нет стабильного идентификатора.",
                )
            if pending_id in seen_ids:
                raise ToolFailure(
                    "materials_unavailable",
                    "В учебных материалах повторяется идентификатор раздела.",
                )

            finish_section()
            seen_ids.add(pending_id)
            current_id = pending_id
            current_title = heading.group(1).strip()
            current_lines = []
            pending_id = None
            continue

        if current_id is not None:
            current_lines.append(line)

    if pending_id is not None:
        raise ToolFailure(
            "materials_unavailable",
            "В учебных материалах нарушена разметка разделов.",
        )

    finish_section()

    if not sections:
        raise ToolFailure("materials_unavailable", "В документе нет разделов.")

    return ParsedDocument(spec=spec, sections=tuple(sections))


class MaterialCorpus:
    """Approved Markdown corpus with stable document and section identifiers."""

    def __init__(
        self,
        root: Path = MATERIALS_ROOT,
        documents: Sequence[DocumentSpec] = STARTER_DOCUMENTS,
    ) -> None:
        self._root = root
        self._specs = tuple(documents)
        self._documents: tuple[ParsedDocument, ...] | None = None

    def _loaded(self) -> tuple[ParsedDocument, ...]:
        if self._documents is not None:
            return self._documents

        documents: list[ParsedDocument] = []
        source_ids: set[str] = set()
        citations: set[str] = set()
        subject_titles: dict[str, str] = {}

        for spec in self._specs:
            if Path(spec.filename).name != spec.filename or spec.source_id in source_ids:
                raise ToolFailure(
                    "materials_unavailable",
                    "Реестр учебных материалов содержит некорректные записи.",
                )

            document = _parse_markdown(spec, self._root)
            source_ids.add(spec.source_id)
            subject_title = subject_titles.setdefault(spec.subject_id, spec.subject_title)
            if subject_title != spec.subject_title:
                raise ToolFailure(
                    "materials_unavailable",
                    "У предмета учебных материалов разные названия.",
                )

            for section in document.sections:
                citation = self._citation(spec.source_id, section.section_id)
                if citation in citations:
                    raise ToolFailure(
                        "materials_unavailable",
                        "В учебных материалах повторяется ссылка на раздел.",
                    )
                citations.add(citation)

            documents.append(document)

        if not documents:
            raise ToolFailure("materials_unavailable", "Корпус учебных материалов пуст.")

        self._documents = tuple(documents)
        return self._documents

    @staticmethod
    def _citation(source_id: str, section_id: str) -> str:
        return f"materials://{source_id}#{section_id}"

    def catalog(self) -> MaterialCatalog:
        documents = [
            MaterialCatalogDocument(
                source_id=document.spec.source_id,
                title=document.spec.title,
                subject_id=document.spec.subject_id,
                subject_title=document.spec.subject_title,
                lecture_id=document.spec.lecture_id,
                lecture_title=document.spec.lecture_title,
                summary=document.spec.summary,
                sections=[
                    MaterialCatalogSection(
                        section_id=section.section_id,
                        title=section.title,
                        citation=self._citation(document.spec.source_id, section.section_id),
                    )
                    for section in document.sections
                ],
            )
            for document in self._loaded()
        ]
        return MaterialCatalog(documents=documents)

    def search(self, arguments: MaterialSearchArguments) -> MaterialSearchResult:
        documents = self._loaded()
        subjects = {document.spec.subject_id: document.spec.subject_title for document in documents}

        if arguments.subject_id is not None and arguments.subject_id not in subjects:
            raise ToolFailure(
                "unknown_subject",
                "Предмет не найден в каталоге учебных материалов.",
            )

        terms = query_terms(arguments.query)
        filtered = (
            document
            for document in documents
            if arguments.subject_id is None or document.spec.subject_id == arguments.subject_id
        )
        hits: list[_ScoredSection] = []

        if terms:
            for document in filtered:
                for section in document.sections:
                    scored = score_section(section, terms)
                    if scored.score > 0:
                        hits.append(
                            _ScoredSection(
                                document=document,
                                section=section,
                                score=scored.score,
                                matched_terms=scored.matched_terms,
                            )
                        )

        hits.sort(
            key=lambda hit: (
                -hit.score,
                hit.document.spec.source_id,
                hit.section.order,
            )
        )
        selected = hits[: arguments.limit]
        results = [
            MaterialSnippet(
                source_id=hit.document.spec.source_id,
                section_id=hit.section.section_id,
                citation=self._citation(hit.document.spec.source_id, hit.section.section_id),
                source_title=hit.document.spec.title,
                section_title=hit.section.title,
                subject_id=hit.document.spec.subject_id,
                subject_title=hit.document.spec.subject_title,
                lecture_id=hit.document.spec.lecture_id,
                lecture_title=hit.document.spec.lecture_title,
                text=excerpt(hit.section.text, hit.matched_terms),
            )
            for hit in selected
        ]

        return MaterialSearchResult(
            status=KnowledgeStatus.OK if results else KnowledgeStatus.EMPTY,
            query=arguments.query,
            subject_id=arguments.subject_id,
            results=results,
            total=len(hits),
            truncated=len(hits) > len(results),
        )
