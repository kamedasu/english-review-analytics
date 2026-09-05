from __future__ import annotations

import re
from dataclasses import dataclass


DEFAULT_RESULT_COUNT = 5
MAX_REQUESTED_RESULTS = 30


@dataclass(frozen=True)
class RagQueryIntent:
    kind: str
    requested_count: int
    recent: bool
    has_explicit_count: bool


def parse_rag_query(query: str) -> RagQueryIntent:
    normalized = query.casefold()
    return RagQueryIntent(
        kind=_intent_kind(normalized),
        requested_count=_requested_count(normalized),
        recent=any(marker in normalized for marker in ("最近", "直近", "recent", "recently", "latest")),
        has_explicit_count=_has_requested_count(normalized),
    )


def _intent_kind(query: str) -> str:
    if _contains_any(query, ("自然なフレーズ", "自然な言い回し", "直された", "より自然", "natural expression", "natural expressions", "corrected", "correction")):
        return "natural_expression"
    if _contains_any(query, ("覚えたほうが", "ネイティブ", "おすすめ", "使えるフレーズ", "使えそうな表現", "useful phrase", "useful phrases", "native expression", "native expressions", "phrases to remember")):
        return "phrase_recommendation"
    if _contains_any(query, ("弱点", "ミス", "苦手", "mistake", "mistakes", "weakness", "weaknesses")):
        return "weakness"
    if _contains_any(query, ("良かったところ", "できるようになった", "成長", "strength", "strengths", "good point", "good points")):
        return "strength"
    return "general_history"


def _requested_count(query: str) -> int:
    match = re.search(r"(?<!\d)(\d{1,3})\s*(?:個|件|phrases?|expressions?)", query, re.IGNORECASE)
    if match is None:
        match = re.search(r"\bgive\s+me\s+(\d{1,3})\b", query, re.IGNORECASE)
    if match is None:
        return DEFAULT_RESULT_COUNT
    return min(int(match.group(1)), MAX_REQUESTED_RESULTS)


def _has_requested_count(query: str) -> bool:
    return _requested_count(query) != DEFAULT_RESULT_COUNT or bool(
        re.search(r"(?<!\d)5\s*(?:個|件|phrases?|expressions?)|\bgive\s+me\s+5\b", query, re.IGNORECASE)
    )


def _contains_any(query: str, markers: tuple[str, ...]) -> bool:
    return any(marker in query for marker in markers)
