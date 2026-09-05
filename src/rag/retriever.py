from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Protocol

from src.config import get_settings
from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from src.rag.models import RagDocument, RagMetadataValue


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
        response = self._chroma_store.query(embeddings[0], k, where)
        return _retrieved_documents(response)


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
