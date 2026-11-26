from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from app.core.schemas.knowledge import MAX_MATERIAL_SNIPPET_CHARS

_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

_STOPWORDS = frozenset(
    {
        "а",
        "без",
        "более",
        "бы",
        "был",
        "была",
        "были",
        "было",
        "быть",
        "в",
        "вам",
        "вас",
        "вдруг",
        "ведь",
        "весь",
        "во",
        "вот",
        "впрочем",
        "все",
        "всегда",
        "всего",
        "всех",
        "всю",
        "вы",
        "где",
        "да",
        "даже",
        "два",
        "для",
        "до",
        "другой",
        "его",
        "ее",
        "ей",
        "ему",
        "если",
        "есть",
        "еще",
        "же",
        "за",
        "зачем",
        "здесь",
        "и",
        "из",
        "или",
        "им",
        "иногда",
        "их",
        "к",
        "как",
        "какая",
        "какой",
        "когда",
        "конечно",
        "который",
        "кто",
        "куда",
        "ли",
        "лучше",
        "между",
        "меня",
        "мне",
        "много",
        "может",
        "можно",
        "мой",
        "моя",
        "мы",
        "на",
        "над",
        "надо",
        "наконец",
        "нас",
        "не",
        "него",
        "нее",
        "ней",
        "нельзя",
        "нет",
        "ни",
        "нибудь",
        "никогда",
        "ним",
        "них",
        "ничего",
        "но",
        "ну",
        "о",
        "об",
        "один",
        "он",
        "она",
        "они",
        "опять",
        "от",
        "перед",
        "по",
        "под",
        "после",
        "потом",
        "потому",
        "почти",
        "при",
        "про",
        "раз",
        "разве",
        "сам",
        "себе",
        "себя",
        "с",
        "со",
        "совсем",
        "так",
        "также",
        "такой",
        "там",
        "тебя",
        "тем",
        "теперь",
        "то",
        "тогда",
        "того",
        "тоже",
        "только",
        "том",
        "тот",
        "тут",
        "ты",
        "у",
        "уж",
        "уже",
        "хоть",
        "чего",
        "чем",
        "через",
        "что",
        "чтоб",
        "чтобы",
        "чуть",
        "эта",
        "эти",
        "это",
        "этого",
        "этом",
        "этот",
        "эту",
        "я",
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "with",
    }
)

_RU_SUFFIXES: tuple[str, ...] = (
    "иями",
    "ями",
    "ами",
    "ыми",
    "ими",
    "иях",
    "ях",
    "ах",
    "ость",
    "ение",
    "ание",
    "ов",
    "ев",
    "ий",
    "ый",
    "ой",
    "ая",
    "ое",
    "ые",
    "ую",
    "юю",
    "ем",
    "ом",
    "ам",
    "ям",
    "ешь",
    "ишь",
    "ете",
    "ите",
    "ют",
    "ут",
    "ат",
    "ят",
    "лся",
    "лась",
    "лись",
    "ться",
    "тся",
    "ть",
    "ся",
    "сь",
    "ей",
    "е",
    "у",
    "ю",
    "я",
    "а",
    "о",
    "ы",
    "и",
    "й",
    "ь",
)


class SearchableSection(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def tokens(self) -> tuple[str, ...]: ...

    @property
    def title_tokens(self) -> tuple[str, ...]: ...

    @property
    def order(self) -> int: ...


@dataclass(frozen=True)
class SectionScore:
    score: float
    matched_terms: tuple[str, ...]


def normalize(value: str) -> str:
    return value.casefold().replace("ё", "е")


def stem(word: str) -> str:
    if word.isascii():
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word

    for suffix in _RU_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]

    return word


def tokenize(value: str) -> tuple[str, ...]:
    result: list[str] = []

    for raw in _WORD.findall(normalize(value)):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        result.append(stem(raw))

    return tuple(result)


def query_terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tokenize(value)))


def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0

    while index < limit and left[index] == right[index]:
        index += 1

    return index


def _token_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4:
        return False

    common = _common_prefix_length(left, right)
    if left.isascii() and right.isascii():
        return common >= 6

    return common >= 5


def score_section(
    section: SearchableSection,
    terms: tuple[str, ...],
) -> SectionScore:
    body_counts = Counter(section.tokens)
    title_counts = Counter(section.title_tokens)
    matched_terms: list[str] = []
    score = 0.0

    for term in terms:
        body_frequency = sum(
            count for token, count in body_counts.items() if _token_matches(term, token)
        )
        title_frequency = sum(
            count for token, count in title_counts.items() if _token_matches(term, token)
        )
        if not body_frequency and not title_frequency:
            continue

        matched_terms.append(term)
        score += 2.0
        score += min(body_frequency, 6) * 0.25
        if title_frequency:
            score += 3.0 + min(title_frequency, 3) * 0.5

    if not matched_terms:
        return SectionScore(score=0.0, matched_terms=())

    score += 2.0 * len(matched_terms) / len(terms)
    return SectionScore(score=score, matched_terms=tuple(matched_terms))


def _first_match_offset(value: str, terms: tuple[str, ...]) -> int:
    for match in _WORD.finditer(value):
        token = stem(normalize(match.group()))
        if any(_token_matches(term, token) for term in terms):
            return match.start()

    return 0


def excerpt(value: str, terms: tuple[str, ...]) -> str:
    compact = " ".join(value.split())
    if len(compact) <= MAX_MATERIAL_SNIPPET_CHARS:
        return compact

    offset = _first_match_offset(compact, terms)
    start = max(0, offset - MAX_MATERIAL_SNIPPET_CHARS // 3)
    if start:
        boundary = compact.find(" ", start, min(len(compact), start + 40))
        if boundary != -1:
            start = boundary + 1

    end = min(len(compact), start + MAX_MATERIAL_SNIPPET_CHARS)
    if end < len(compact):
        boundary = compact.rfind(" ", start, end)
        if boundary > start:
            end = boundary

    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    allowance = MAX_MATERIAL_SNIPPET_CHARS - len(prefix) - len(suffix)
    fragment = compact[start:end].strip()[:allowance].rstrip()
    return f"{prefix}{fragment}{suffix}"
