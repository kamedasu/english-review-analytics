from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from src.config import get_settings
from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from src.rag.models import RagDocument, RagMetadataValue
from src.rag.query_intent import RagQueryIntent, parse_rag_query


TYPE_DISTANCE_PENALTIES = {
    "more_natural_expression": 0.0,
    "phrase_card": 0.015,
    "weak_point": 0.03,
    "good_point": 0.045,
}
"""Small deterministic offsets that favour concrete learning records without replacing relevance."""

LEARNING_DOCUMENT_TYPES = frozenset({"more_natural_expression", "phrase_card"})
EXPRESSION_QUERY_MARKERS = (
    "表現",
    "修正",
    "直され",
    "言い換え",
    "具体例",
    "実際にどんな英語",
    "どんな英語",
    "correction",
    "corrected",
    "expression",
    "phrase",
    "example",
    "before",
    "after",
)
EXPRESSION_RELEVANCE_DISTANCE_MARGIN = 0.4
RECENT_WINDOWS_DAYS = (60, 120, 180)
MAX_CANDIDATES_PER_TYPE = 400
MAX_RECENCY_REFERENCE_CANDIDATES = 2000
ALL_DOCUMENT_TYPES = ("more_natural_expression", "phrase_card", "weak_point", "good_point")

INTENT_DOCUMENT_TYPES = {
    "natural_expression": ("more_natural_expression", "phrase_card"),
    "phrase_recommendation": ("phrase_card", "more_natural_expression"),
    "weakness": ("weak_point", "more_natural_expression"),
    "strength": ("good_point",),
}


@dataclass(frozen=True)
class RetrievedDocument:
    document: RagDocument
    distance: float


class QueryStore(Protocol):
    def query(
        self,
        query_embedding: list[float],
        k: int,
        where: dict[str, RagMetadataValue] | None = None,
    ) -> dict[str, list[list[object]]]:
        ...


class RagRetriever:
    """Embeds a query and retrieves semantic matches from an existing Chroma index."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        chroma_store: QueryStore | None = None,
    ) -> None:
        self._embedding_provider = embedding_provider or OpenAIEmbeddingProvider.from_settings()
        if chroma_store is None:
            settings = get_settings()
            chroma_store = ChromaStore(settings.rag_chroma_dir, settings.rag_collection_name)
        self._chroma_store = chroma_store

    def retrieve(
        self,
        query: str,
        k: int = 5,
        where: dict[str, RagMetadataValue] | None = None,
    ) -> list[RetrievedDocument]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Query must not be blank.")
        if k <= 0:
            raise ValueError("k must be greater than zero.")

        embeddings = self._embedding_provider.embed_texts([clean_query])
        if len(embeddings) != 1:
            raise ValueError(f"Query embedding count mismatch: expected 1, received {len(embeddings)}.")
        latest_index_date: date | None = None
        intent = parse_rag_query(clean_query)
        if where is None and (intent.requires_reference_date or intent.recent):
            latest_index_date = _latest_index_document_date(self._chroma_store, embeddings[0])
            intent = parse_rag_query(clean_query, latest_index_date)
        if where is None and (intent.kind in INTENT_DOCUMENT_TYPES or intent.start_date is not None):
            return _retrieve_for_intent(self._chroma_store, embeddings[0], intent, k, latest_index_date)

        candidate_k = k if where is not None else _candidate_count(k)
        response = self._chroma_store.query(embeddings[0], candidate_k, where)
        results = _retrieved_documents(response)
        if where is not None:
            return _apply_hard_date_constraint(results, intent)[:k]
        if _is_expression_focused_query(clean_query):
            learning_candidates = _learning_type_candidates(self._chroma_store, embeddings[0], k)
            return _prioritize_expression_documents(results, learning_candidates, k)
        return _prioritize_learning_documents(results, k)


def _candidate_count(k: int) -> int:
    return k * 4


def _retrieve_for_intent(
    chroma_store: QueryStore,
    query_embedding: list[float],
    intent: RagQueryIntent,
    k: int,
    latest_index_date: date | None = None,
) -> list[RetrievedDocument]:
    """Retrieve only the document types appropriate for an explicit learning intent."""
    candidates: list[RetrievedDocument] = []
    candidate_k = min(max(k * 4, 60), MAX_CANDIDATES_PER_TYPE)
    for document_type in _intent_document_types(intent):
        response = chroma_store.query(query_embedding, candidate_k, {"type": document_type})
        candidates.extend(_retrieved_documents(response))
    if intent.recent and latest_index_date is None:
        latest_index_date = _latest_index_document_date(chroma_store, query_embedding)
    return _select_intent_documents(candidates, intent, k, latest_index_date)


def _select_intent_documents(
    candidates: list[RetrievedDocument],
    intent: RagQueryIntent,
    k: int,
    latest_index_date: date | None = None,
) -> list[RetrievedDocument]:
    unique_candidates = _deduplicate_learning_documents(candidates)
    if not unique_candidates:
        return []

    if _has_hard_date_constraint(intent):
        in_range = _apply_hard_date_constraint(unique_candidates, intent)
        return _sort_intent_documents(in_range, intent)[:k]

    if intent.recent:
        latest_date = latest_index_date or max((_document_date(candidate) for candidate in unique_candidates), default=None)
        if latest_date is not None:
            for window_days in (*RECENT_WINDOWS_DAYS, None):
                filtered = _documents_in_recent_window(unique_candidates, latest_date, window_days)
                if len(filtered) >= k or window_days is None:
                    return _sort_intent_documents(filtered, intent)[:k]
    return _sort_intent_documents(unique_candidates, intent)[:k]


def _has_hard_date_constraint(intent: RagQueryIntent) -> bool:
    return intent.start_date is not None and intent.end_date is not None


def _apply_hard_date_constraint(
    candidates: list[RetrievedDocument],
    intent: RagQueryIntent,
) -> list[RetrievedDocument]:
    """Never reintroduce out-of-range documents after an explicit date constraint."""
    if not _has_hard_date_constraint(intent):
        return candidates
    assert intent.start_date is not None and intent.end_date is not None
    return [
        candidate
        for candidate in candidates
        if (candidate_date := _document_date(candidate)) is not None
        and intent.start_date <= candidate_date <= intent.end_date
    ]


def _latest_index_document_date(chroma_store: QueryStore, query_embedding: list[float]) -> date | None:
    """Read metadata only through the existing query API to anchor 'recent' to index data, not wall-clock time."""
    response = chroma_store.query(query_embedding, MAX_RECENCY_REFERENCE_CANDIDATES)
    return max((_document_date(candidate) for candidate in _retrieved_documents(response)), default=None)


def _documents_in_recent_window(
    candidates: list[RetrievedDocument],
    latest_date: date,
    window_days: int | None,
) -> list[RetrievedDocument]:
    if window_days is None:
        return candidates
    cutoff = latest_date - timedelta(days=window_days)
    return [
        candidate
        for candidate in candidates
        if (candidate_date := _document_date(candidate)) is not None and candidate_date >= cutoff
    ]


def _sort_intent_documents(candidates: list[RetrievedDocument], intent: RagQueryIntent) -> list[RetrievedDocument]:
    type_order = {document_type: index for index, document_type in enumerate(_intent_document_types(intent))}
    if intent.recent:
        return sorted(
            candidates,
            key=lambda candidate: (
                type_order.get(str(candidate.document.metadata.get("type", "")), len(type_order)),
                -_document_date_ordinal(candidate),
                candidate.distance,
            ),
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            type_order.get(str(candidate.document.metadata.get("type", "")), len(type_order)),
            candidate.distance,
        ),
    )


def _intent_document_types(intent: RagQueryIntent) -> tuple[str, ...]:
    return INTENT_DOCUMENT_TYPES.get(intent.kind, ALL_DOCUMENT_TYPES)


def _document_date(candidate: RetrievedDocument) -> date | None:
    value = candidate.document.metadata.get("date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _document_date_ordinal(candidate: RetrievedDocument) -> int:
    document_date = _document_date(candidate)
    return document_date.toordinal() if document_date is not None else 0


def _deduplicate_learning_documents(candidates: list[RetrievedDocument]) -> list[RetrievedDocument]:
    unique: list[RetrievedDocument] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        key = _learning_document_key(candidate)
        if key not in seen_keys:
            unique.append(candidate)
            seen_keys.add(key)
    return unique


def _learning_document_key(candidate: RetrievedDocument) -> str:
    text = candidate.document.text
    document_type = str(candidate.document.metadata.get("type", ""))
    prefix = "More natural:" if document_type == "more_natural_expression" else "Phrase:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return _normalize_phrase_key(line.removeprefix(prefix))
    return candidate.document.id


def _normalize_phrase_key(value: str) -> str:
    return "".join(character for character in value.casefold().strip() if character.isalnum() or character.isspace()).strip()


def _is_expression_focused_query(query: str) -> bool:
    normalized = query.casefold()
    return any(marker in normalized for marker in EXPRESSION_QUERY_MARKERS)


def _learning_type_candidates(
    chroma_store: QueryStore,
    query_embedding: list[float],
    k: int,
) -> list[RetrievedDocument]:
    candidates: list[RetrievedDocument] = []
    for document_type in ("more_natural_expression", "phrase_card"):
        response = chroma_store.query(query_embedding, k, {"type": document_type})
        candidates.extend(_retrieved_documents(response))
    return candidates


def _prioritize_expression_documents(
    semantic_candidates: list[RetrievedDocument],
    learning_candidates: list[RetrievedDocument],
    k: int,
) -> list[RetrievedDocument]:
    """Reserve up to three sufficiently relevant concrete-expression sources for expression questions."""
    if not semantic_candidates:
        return []

    best_distance = min(candidate.distance for candidate in semantic_candidates)
    maximum_learning_distance = best_distance + EXPRESSION_RELEVANCE_DISTANCE_MARGIN
    concrete_candidates = [
        candidate
        for candidate in _unique_documents(learning_candidates)
        if candidate.distance <= maximum_learning_distance
    ]
    concrete_candidates.sort(
        key=lambda candidate: (
            candidate.distance + TYPE_DISTANCE_PENALTIES.get(
                str(candidate.document.metadata.get("type", "")),
                TYPE_DISTANCE_PENALTIES["good_point"],
            ),
        )
    )
    concrete_limit = min(max(k - 2, 0), len(concrete_candidates))
    selected = concrete_candidates[:concrete_limit]
    selected_ids = {candidate.document.id for candidate in selected}

    support_candidates = [
        candidate
        for candidate in _unique_documents(semantic_candidates)
        if candidate.document.id not in selected_ids
        and str(candidate.document.metadata.get("type", "")) not in LEARNING_DOCUMENT_TYPES
    ]
    support_candidates.sort(key=lambda candidate: candidate.distance)
    selected.extend(support_candidates[: k - len(selected)])
    if len(selected) < k:
        selected.extend(
            candidate
            for candidate in _prioritize_learning_documents(semantic_candidates, k)
            if candidate.document.id not in {item.document.id for item in selected}
        )
    return selected[:k]


def _unique_documents(candidates: list[RetrievedDocument]) -> list[RetrievedDocument]:
    unique: list[RetrievedDocument] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        if candidate.document.id not in seen_ids:
            unique.append(candidate)
            seen_ids.add(candidate.document.id)
    return unique


def _prioritize_learning_documents(
    candidates: list[RetrievedDocument], k: int,
) -> list[RetrievedDocument]:
    """Keep Chroma's semantic candidates, then use a modest type-aware tie-breaker."""
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            item[1].distance + TYPE_DISTANCE_PENALTIES.get(
                str(item[1].document.metadata.get("type", "")),
                TYPE_DISTANCE_PENALTIES["good_point"],
            ),
            item[0],
        ),
    )
    return [candidate for _, candidate in ranked[:k]]


def _retrieved_documents(response: dict[str, list[list[object]]]) -> list[RetrievedDocument]:
    ids = _first_result_set(response, "ids")
    documents = _first_result_set(response, "documents")
    metadatas = _first_result_set(response, "metadatas")
    distances = _first_result_set(response, "distances")
    lengths = {len(ids), len(documents), len(metadatas), len(distances)}
    if len(lengths) != 1:
        raise ValueError("Chroma query returned mismatched result field counts.")

    results: list[RetrievedDocument] = []
    for document_id, text, metadata, distance in zip(ids, documents, metadatas, distances):
        if not isinstance(document_id, str) or not isinstance(text, str) or not isinstance(metadata, dict):
            raise ValueError("Chroma query returned an invalid document result.")
        results.append(
            RetrievedDocument(
                document=RagDocument(id=document_id, text=text, metadata=metadata),
                distance=float(distance),
            )
        )
    return results


def _first_result_set(response: dict[str, list[list[object]]], key: str) -> list[object]:
    rows = response.get(key)
    if not rows:
        return []
    return rows[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieve documents from the local English Review Chroma index.")
    parser.add_argument("query", help="Question or search phrase")
    parser.add_argument("--k", type=int, default=5, help="Number of results to return (default: 5)")
    args = parser.parse_args()
    try:
        results = RagRetriever().retrieve(args.query, args.k)
    except (ValueError, RagIndexNotFoundError) as exc:
        parser.error(str(exc))

    print(f"Query:\n{args.query}\n\nTop {len(results)}:")
    for index, result in enumerate(results, start=1):
        metadata = result.document.metadata
        print(
            f"\n{index}.\n"
            f"distance: {result.distance}\n"
            f"type: {metadata.get('type', '')}\n"
            f"date: {metadata.get('date', '')}\n"
            f"topic: {metadata.get('topic', '')}\n"
            f"id: {result.document.id}\n"
            f"text:\n{result.document.text}"
        )


if __name__ == "__main__":
    main()
