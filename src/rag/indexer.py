from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import argparse

from src.config import get_settings
from src.data_loader import load_local_reviews
from src.models import Review
from src.rag.chroma_store import ChromaStore, RagIndexRebuildRequiredError
from src.rag.document_builder import build_rag_documents
from src.rag.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from src.rag.models import RagDocument


RAG_DOCUMENT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class RagIndexResult:
    review_count: int
    document_count: int
    embedded_count: int
    stored_count: int
    collection_name: str
    chroma_path: str
    type_counts: dict[str, int]
    added_count: int = 0
    changed_count: int = 0
    deleted_count: int = 0
    unchanged_count: int = 0
    initial_build: bool = False


@dataclass(frozen=True)
class RagIndexDiff:
    added: list[RagDocument]
    changed: list[RagDocument]
    deleted_ids: list[str]
    unchanged_count: int

    @property
    def embedding_documents(self) -> list[RagDocument]:
        return [*self.added, *self.changed]


def rebuild_rag_index(
    reviews: list[Review],
    embedding_provider: EmbeddingProvider | None = None,
    chroma_store: ChromaStore | None = None,
) -> RagIndexResult:
    """Rebuild the local Chroma collection from already persisted reviews only."""
    documents = build_rag_documents(reviews)
    settings = get_settings()
    store = chroma_store or ChromaStore(settings.rag_chroma_dir, settings.rag_collection_name)
    return _full_rebuild_documents(documents, len(reviews), embedding_provider, store, settings.openai_embedding_model)


def compute_rag_index_diff(
    current_documents: list[RagDocument],
    indexed_documents: list[RagDocument],
) -> RagIndexDiff:
    """Pure, deterministic comparison of Chroma source fields; embeddings are intentionally ignored."""
    current_by_id = _documents_by_id(current_documents, "current")
    indexed_by_id = _documents_by_id(indexed_documents, "indexed")
    added = [document for document in current_documents if document.id not in indexed_by_id]
    changed = [
        document
        for document in current_documents
        if document.id in indexed_by_id
        and (document.text != indexed_by_id[document.id].text or document.metadata != indexed_by_id[document.id].metadata)
    ]
    deleted_ids = sorted(document_id for document_id in indexed_by_id if document_id not in current_by_id)
    unchanged_count = len(current_documents) - len(added) - len(changed)
    return RagIndexDiff(added=added, changed=changed, deleted_ids=deleted_ids, unchanged_count=unchanged_count)


def update_rag_index_incrementally(
    reviews: list[Review],
    embedding_provider: EmbeddingProvider | None = None,
    chroma_store: ChromaStore | None = None,
) -> RagIndexResult:
    """Apply only added/changed/deleted source fields to an existing local Chroma collection."""
    documents = build_rag_documents(reviews)
    _documents_by_id(documents, "current")
    settings = get_settings()
    store = chroma_store or ChromaStore(settings.rag_chroma_dir, settings.rag_collection_name)
    if not store.collection_exists():
        return _full_rebuild_documents(
            documents,
            len(reviews),
            embedding_provider,
            store,
            settings.openai_embedding_model,
            initial_build=True,
        )

    _require_compatible_collection(store.collection_metadata(), settings.openai_embedding_model)
    diff = compute_rag_index_diff(documents, store.get_all_documents())
    embedding_documents = diff.embedding_documents
    embeddings: list[list[float]] = []
    if embedding_documents:
        provider = embedding_provider or OpenAIEmbeddingProvider.from_settings()
        embeddings = provider.embed_texts([document.text for document in embedding_documents])
        if len(embeddings) != len(embedding_documents):
            raise ValueError(
                f"Embedding count mismatch: {len(embeddings)} vectors for {len(embedding_documents)} RAG documents."
            )

    # All new vectors are ready before any stored document is deleted.
    stored_count = store.apply_incremental(
        delete_ids=[*diff.deleted_ids, *(document.id for document in diff.changed)],
        documents=embedding_documents,
        embeddings=embeddings,
    )
    if stored_count != len(documents):
        raise ValueError(f"Chroma stored count mismatch: {stored_count} stored for {len(documents)} documents.")
    return _index_result(
        review_count=len(reviews),
        documents=documents,
        embedded_count=len(embeddings),
        stored_count=stored_count,
        store=store,
        added_count=len(diff.added),
        changed_count=len(diff.changed),
        deleted_count=len(diff.deleted_ids),
        unchanged_count=diff.unchanged_count,
    )


def _full_rebuild_documents(
    documents: list[RagDocument],
    review_count: int,
    embedding_provider: EmbeddingProvider | None,
    store: ChromaStore,
    embedding_model: str,
    initial_build: bool = False,
) -> RagIndexResult:
    _documents_by_id(documents, "current")
    provider = embedding_provider or OpenAIEmbeddingProvider.from_settings()
    embeddings = provider.embed_texts([document.text for document in documents])
    if len(embeddings) != len(documents):
        raise ValueError(f"Embedding count mismatch: {len(embeddings)} vectors for {len(documents)} RAG documents.")
    stored_count = store.rebuild(documents, embeddings, _collection_metadata(embedding_model))
    if stored_count != len(documents):
        raise ValueError(f"Chroma stored count mismatch: {stored_count} stored for {len(documents)} documents.")
    return _index_result(
        review_count=review_count,
        documents=documents,
        embedded_count=len(embeddings),
        stored_count=stored_count,
        store=store,
        added_count=len(documents) if initial_build else 0,
        changed_count=0,
        deleted_count=0,
        unchanged_count=0,
        initial_build=initial_build,
    )


def _documents_by_id(documents: list[RagDocument], label: str) -> dict[str, RagDocument]:
    result: dict[str, RagDocument] = {}
    for document in documents:
        if document.id in result:
            raise ValueError(f"Duplicate {label} RAG document id: {document.id}")
        result[document.id] = document
    return result


def _collection_metadata(embedding_model: str) -> dict[str, str]:
    return {"embedding_model": embedding_model, "rag_document_schema_version": RAG_DOCUMENT_SCHEMA_VERSION}


def _require_compatible_collection(metadata: dict[str, str], embedding_model: str) -> None:
    expected = _collection_metadata(embedding_model)
    for field, expected_value in expected.items():
        actual_value = metadata.get(field)
        if actual_value not in (None, "", expected_value):
            raise RagIndexRebuildRequiredError(
                f"RAG index {field} is {actual_value!r}, expected {expected_value!r}. Run: python -m src.rag.indexer"
            )


def _index_result(
    *, review_count: int, documents: list[RagDocument], embedded_count: int, stored_count: int,
    store: ChromaStore, added_count: int, changed_count: int, deleted_count: int,
    unchanged_count: int, initial_build: bool = False,
) -> RagIndexResult:
    return RagIndexResult(
        review_count=review_count,
        document_count=len(documents),
        embedded_count=embedded_count,
        stored_count=stored_count,
        collection_name=store.collection_name,
        chroma_path=str(store.path),
        type_counts=dict(Counter(document.metadata["type"] for document in documents)),
        added_count=added_count,
        changed_count=changed_count,
        deleted_count=deleted_count,
        unchanged_count=unchanged_count,
        initial_build=initial_build,
    )


def main() -> None:
    # This reads saved reviews through the existing storage abstraction and never syncs Notion.
    parser = argparse.ArgumentParser(description="Build or incrementally update the local English Review Chroma index.")
    parser.add_argument("--incremental", action="store_true", help="Embed and apply only added or changed documents")
    args = parser.parse_args()
    reviews = load_local_reviews().reviews
    result = update_rag_index_incrementally(reviews) if args.incremental else rebuild_rag_index(reviews)
    if result.initial_build:
        print("Collection not found. Creating initial index.")
    print(f"Reviews: {result.review_count}")
    print(f"RAG Documents: {result.document_count}")
    print(f"Added: {result.added_count}")
    print(f"Changed: {result.changed_count}")
    print(f"Deleted: {result.deleted_count}")
    print(f"Unchanged: {result.unchanged_count}")
    print(f"Embedded: {result.embedded_count}")
    print(f"Stored in Chroma: {result.stored_count}")
    print(f"Collection: {result.collection_name}")
    print(f"Path: {result.chroma_path}")
    for document_type in ("good_point", "weak_point", "more_natural_expression", "phrase_card"):
        print(f"{document_type}: {result.type_counts.get(document_type, 0)}")


if __name__ == "__main__":
    main()
