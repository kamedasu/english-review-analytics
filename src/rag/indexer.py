from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.config import get_settings
from src.data_loader import load_local_reviews
from src.models import Review
from src.rag.chroma_store import ChromaStore
from src.rag.document_builder import build_rag_documents
from src.rag.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider


@dataclass(frozen=True)
class RagIndexResult:
    review_count: int
    document_count: int
    embedded_count: int
    stored_count: int
    collection_name: str
    chroma_path: str
    type_counts: dict[str, int]


def rebuild_rag_index(
    reviews: list[Review],
    embedding_provider: EmbeddingProvider | None = None,
    chroma_store: ChromaStore | None = None,
) -> RagIndexResult:
    """Rebuild the local Chroma collection from already persisted reviews only."""
    documents = build_rag_documents(reviews)
    provider = embedding_provider or OpenAIEmbeddingProvider.from_settings()
    embeddings = provider.embed_texts([document.text for document in documents])
    if len(embeddings) != len(documents):
        raise ValueError(
            f"Embedding count mismatch: {len(embeddings)} vectors for {len(documents)} RAG documents."
        )

    settings = get_settings()
    store = chroma_store or ChromaStore(settings.rag_chroma_dir, settings.rag_collection_name)
    stored_count = store.rebuild(documents, embeddings)
    if stored_count != len(documents):
        raise ValueError(f"Chroma stored count mismatch: {stored_count} stored for {len(documents)} documents.")
    return RagIndexResult(
        review_count=len(reviews),
        document_count=len(documents),
        embedded_count=len(embeddings),
        stored_count=stored_count,
        collection_name=store.collection_name,
        chroma_path=str(store.path),
        type_counts=dict(Counter(document.metadata["type"] for document in documents)),
    )


def main() -> None:
    # This reads saved reviews through the existing storage abstraction and never syncs Notion.
    result = rebuild_rag_index(load_local_reviews().reviews)
    print(f"Reviews: {result.review_count}")
    print(f"RAG Documents: {result.document_count}")
    print(f"Embedded: {result.embedded_count}")
    print(f"Stored in Chroma: {result.stored_count}")
    print(f"Collection: {result.collection_name}")
    print(f"Path: {result.chroma_path}")
    for document_type in ("good_point", "weak_point", "more_natural_expression", "phrase_card"):
        print(f"{document_type}: {result.type_counts.get(document_type, 0)}")


if __name__ == "__main__":
    main()
