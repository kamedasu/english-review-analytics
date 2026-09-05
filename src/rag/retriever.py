from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Protocol

from src.config import get_settings
from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from src.rag.models import RagDocument, RagMetadataValue


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
        candidate_k = k if where is not None else _candidate_count(k)
        response = self._chroma_store.query(embeddings[0], candidate_k, where)
        results = _retrieved_documents(response)
        if where is not None:
            return results[:k]
        if _is_expression_focused_query(clean_query):
            learning_candidates = _learning_type_candidates(self._chroma_store, embeddings[0], k)
            return _prioritize_expression_documents(results, learning_candidates, k)
        return _prioritize_learning_documents(results, k)


def _candidate_count(k: int) -> int:
    return k * 4


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
