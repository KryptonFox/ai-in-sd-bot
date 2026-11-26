import re
from collections.abc import Sequence

from app.core.schemas.agent import AgentDecision, AgentResult
from app.core.schemas.knowledge import MaterialSnippet
from app.core.schemas.specialist import KnowledgeFact, SpecialistFact

_REFERENCE = re.compile(r"(?:https?://|materials?://)[^\s<>()\[\]`]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?\"'»*_"


def _clean_reference(value: str) -> str:
    return value.rstrip(_TRAILING_PUNCTUATION)


def ground_knowledge_response(
    result: AgentResult,
    facts: Sequence[SpecialistFact],
    attempted: set[str],
) -> AgentResult:
    if result.decision in {AgentDecision.ERROR, AgentDecision.CLARIFY}:
        return result

    references: dict[str, str] = {}
    source_kinds: dict[str, str] = {}
    for fact in facts:
        if not isinstance(fact, KnowledgeFact):
            continue
        source = fact.source
        if isinstance(source, MaterialSnippet):
            references[source.citation] = f"{source.lecture_title}, {source.section_title}"
            source_kinds[source.citation] = source.source_type
        else:
            date = f" ({source.published_at})" if source.published_at else ""
            references[source.url] = f"{source.title}{date}"
            source_kinds[source.url] = source.source_type

    if not references:
        if "search_web" in attempted and "search_materials" in attempted:
            return result.model_copy(
                update={
                    "text": "В учебных материалах и по веб-поиску "
                    "подходящих источников не нашлось.",
                    "reason": "empty_sources",
                }
            )
        if "search_web" in attempted:
            return result.model_copy(
                update={
                    "text": "По веб-запросу подходящих источников не нашлось.",
                    "reason": "empty_sources",
                }
            )
        if "search_materials" in attempted:
            return result.model_copy(
                update={
                    "text": "В учебных материалах подходящих фрагментов нет. Поискать в интернете?",
                    "reason": "web_search_offer",
                }
            )
        return result.model_copy(
            update={
                "text": "Уточните тему или название лекции для поиска по учебным материалам.",
                "decision": AgentDecision.CLARIFY,
                "reason": "sources_required",
            }
        )

    cited: list[str] = []
    for match in _REFERENCE.finditer(result.text):
        uri = _clean_reference(match.group())
        if uri not in references:
            return result.model_copy(
                update={
                    "text": "В ответе появилась ссылка, которой не было в результатах поиска. "
                    "Попробуйте уточнить запрос.",
                    "decision": AgentDecision.ERROR,
                    "reason": "unverified_source",
                }
            )
        cited.append(uri)

    limit = 6 if len(set(source_kinds.values())) > 1 else 4
    selected_uris = list(dict.fromkeys([*cited, *references]))[:limit]
    source_lines = [f"• {references[uri]}\n  {uri}" for uri in selected_uris]
    return result.model_copy(
        update={"text": result.text + "\n\nИсточники\n" + "\n".join(source_lines)}
    )
