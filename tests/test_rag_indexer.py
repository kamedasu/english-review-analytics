from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.models import PhraseCard, Review
from src.rag.chroma_store import ChromaStore, RagIndexRebuildRequiredError
from src.rag.embeddings import OpenAIEmbeddingProvider
from src.rag.indexer import compute_rag_index_diff, rebuild_rag_index, update_rag_index_incrementally
from src.rag.models import RagDocument


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts = texts
        return [[float(index)] for index, _ in enumerate(texts)]


class WrongCountEmbeddingProvider:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0]]


class FailingEmbeddingProvider:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts = texts
        raise RuntimeError("embedding failed")


class MemoryIncrementalStore:
    def __init__(self, documents: list[RagDocument] | None = None, metadata: dict[str, str] | None = None) -> None:
        self.collection_name = "english_review_documents"
        self.path = Path("unused")
        self.documents = {document.id: document for document in documents or []}
        self.metadata = metadata or {}
        self.exists = documents is not None
        self.apply_calls: list[tuple[list[str], list[str]]] = []

    def collection_exists(self) -> bool:
        return self.exists

    def collection_metadata(self) -> dict[str, str]:
        return self.metadata

    def get_all_documents(self) -> list[RagDocument]:
        return list(self.documents.values())

    def apply_incremental(self, delete_ids, documents, embeddings) -> int:
        self.apply_calls.append((list(delete_ids), [document.id for document in documents]))
        for document_id in delete_ids:
            self.documents.pop(document_id, None)
        for document in documents:
            self.documents[document.id] = document
        return len(self.documents)

    def rebuild(self, documents, embeddings, collection_metadata=None) -> int:
        self.exists = True
        self.metadata = collection_metadata or {}
        self.documents = {document.id: document for document in documents}
        return len(self.documents)


class FakeCollection:
    def __init__(self) -> None:
        self.added: dict = {}
        self.metadata: dict = {}

    def add(self, **kwargs) -> None:
        self.added = kwargs

    def count(self) -> int:
        return len(self.added.get("ids", []))


class FakeNamedCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeChromaClient:
    def __init__(self, names: list[str] | None = None) -> None:
        self.names = names or []
        self.deleted: list[str] = []
        self.created_name = ""
        self.collection = FakeCollection()

    def list_collections(self):
        return [FakeNamedCollection(name) for name in self.names]

    def delete_collection(self, name: str) -> None:
        self.deleted.append(name)

    def create_collection(self, name: str, embedding_function=None, metadata=None) -> FakeCollection:
        self.created_name = name
        self.collection.metadata = metadata or {}
        return self.collection


def _review() -> Review:
    return Review(
        review_id="review-1", date=date(2026, 1, 2), topic="test",
        good_points=["A good point"], weak_points=["A weak point"],
        phrase_cards=[PhraseCard(phrase="catch up", meaning="talk")],
        raw_markdown="Entire raw review must not be indexed",
    )


def _document(document_id: str, text: str, metadata: dict | None = None) -> RagDocument:
    return RagDocument(document_id, text, metadata or {"type": "phrase_card"})


class RagIndexerTest(unittest.TestCase):
    def test_rebuild_embeds_document_texts_and_stores_document_fields(self) -> None:
        provider = FakeEmbeddingProvider()
        client = FakeChromaClient(names=["english_review_documents", "keep-me"])
        store = ChromaStore(path="unused", collection_name="english_review_documents", client=client)

        result = rebuild_rag_index([_review()], provider, store)

        self.assertEqual(result.document_count, 3)
        self.assertEqual(provider.texts, ["Good point: A good point", "Weak point: A weak point", "Phrase: catch up\nMeaning: talk"])
        self.assertNotIn("Entire raw review", provider.texts)
        self.assertEqual(client.deleted, ["english_review_documents"])
        self.assertNotIn("keep-me", client.deleted)
        self.assertEqual(client.created_name, "english_review_documents")
        self.assertEqual(client.collection.added["ids"], ["review-1:good_point:0", "review-1:weak_point:0", "review-1:phrase_card:0"])
        self.assertEqual(client.collection.added["documents"], provider.texts)
        self.assertEqual(client.collection.added["metadatas"][0], {
            "type": "good_point", "review_id": "review-1", "date": "2026-01-02", "year": 2026,
            "month": "2026-01", "topic": "test", "review_type": "normal", "item_index": 0,
        })
        self.assertEqual(result.stored_count, 3)

    def test_rejects_embedding_count_mismatch(self) -> None:
        store = ChromaStore(path="unused", collection_name="english_review_documents", client=FakeChromaClient())
        with self.assertRaisesRegex(ValueError, "Embedding count mismatch"):
            rebuild_rag_index([_review()], WrongCountEmbeddingProvider(), store)

    def test_store_rejects_mismatched_document_and_embedding_counts(self) -> None:
        store = ChromaStore(path="unused", collection_name="english_review_documents", client=FakeChromaClient())
        with self.assertRaisesRegex(ValueError, "Document and embedding counts must match"):
            store.rebuild([RagDocument("id", "text", {})], [])

    def test_indexer_calls_document_builder(self) -> None:
        document = RagDocument("built:0", "built text", {"type": "good_point"})
        provider = FakeEmbeddingProvider()
        store = ChromaStore(path="unused", collection_name="english_review_documents", client=FakeChromaClient())
        reviews = [_review()]

        with patch("src.rag.indexer.build_rag_documents", return_value=[document]) as builder:
            rebuild_rag_index(reviews, provider, store)

        builder.assert_called_once_with(reviews)
        self.assertEqual(provider.texts, ["built text"])

    def test_chroma_data_is_gitignored(self) -> None:
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/chroma/", gitignore)

    def test_embedding_provider_batches_and_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            OpenAIEmbeddingProvider(api_key="")

        provider = OpenAIEmbeddingProvider(api_key="test", batch_size=2)
        batches: list[list[str]] = []
        provider._embed_batch = lambda batch: batches.append(batch) or [[0.0] for _ in batch]  # type: ignore[method-assign]
        self.assertEqual(provider.embed_texts(["one", "two", "three"]), [[0.0], [0.0], [0.0]])
        self.assertEqual(batches, [["one", "two"], ["three"]])

    def test_compute_diff_classifies_added_deleted_changed_and_unchanged(self) -> None:
        current = [_document("a", "same"), _document("b", "new text"), _document("d", "added")]
        indexed = [_document("a", "same"), _document("b", "old text"), _document("c", "deleted")]

        diff = compute_rag_index_diff(current, indexed)

        self.assertEqual([document.id for document in diff.added], ["d"])
        self.assertEqual([document.id for document in diff.changed], ["b"])
        self.assertEqual(diff.deleted_ids, ["c"])
        self.assertEqual(diff.unchanged_count, 1)

    def test_compute_diff_detects_metadata_changes_but_ignores_dict_order(self) -> None:
        current = [_document("same", "text", {"type": "phrase_card", "date": "2026-01-01"})]
        same_different_order = [_document("same", "text", {"date": "2026-01-01", "type": "phrase_card"})]
        changed = [_document("same", "text", {"type": "phrase_card", "date": "2026-01-02"})]

        self.assertEqual(compute_rag_index_diff(current, same_different_order).unchanged_count, 1)
        self.assertEqual([document.id for document in compute_rag_index_diff(current, changed).changed], ["same"])

    def test_incremental_embeds_only_added_and_changed_and_leaves_unchanged_untouched(self) -> None:
        indexed = [_document("a", "same"), _document("b", "old"), _document("c", "remove")]
        current = [_document("a", "same"), _document("b", "new"), _document("d", "add")]
        store = MemoryIncrementalStore(indexed)
        provider = FakeEmbeddingProvider()

        with patch("src.rag.indexer.build_rag_documents", return_value=current):
            result = update_rag_index_incrementally([_review()], provider, store)

        self.assertEqual(provider.texts, ["add", "new"])
        self.assertEqual(store.apply_calls, [(["c", "b"], ["d", "b"])])
        self.assertEqual(set(store.documents), {"a", "b", "d"})
        self.assertEqual(result.embedded_count, 2)
        self.assertEqual((result.added_count, result.changed_count, result.deleted_count, result.unchanged_count), (1, 1, 1, 1))

    def test_incremental_no_op_and_deleted_only_do_not_embed(self) -> None:
        current = [_document("a", "same")]
        provider = FakeEmbeddingProvider()
        store = MemoryIncrementalStore([_document("a", "same")])
        with patch("src.rag.indexer.build_rag_documents", return_value=current):
            no_op = update_rag_index_incrementally([_review()], provider, store)
        self.assertEqual(provider.texts, [])
        self.assertEqual(no_op.embedded_count, 0)

        deleted_store = MemoryIncrementalStore([_document("a", "same"), _document("b", "removed")])
        with patch("src.rag.indexer.build_rag_documents", return_value=current):
            deleted_only = update_rag_index_incrementally([_review()], provider, deleted_store)
        self.assertEqual(provider.texts, [])
        self.assertEqual(deleted_only.deleted_count, 1)
        self.assertEqual(deleted_store.apply_calls, [(["b"], [])])

    def test_embedding_failure_does_not_mutate_collection(self) -> None:
        store = MemoryIncrementalStore([_document("a", "old")])
        with patch("src.rag.indexer.build_rag_documents", return_value=[_document("a", "new")]):
            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                update_rag_index_incrementally([_review()], FailingEmbeddingProvider(), store)
        self.assertEqual(store.apply_calls, [])
        self.assertEqual(store.documents["a"].text, "old")

    def test_incremental_creates_initial_collection_and_rejects_duplicate_ids(self) -> None:
        provider = FakeEmbeddingProvider()
        initial_store = MemoryIncrementalStore()
        current = [_document("a", "initial")]
        with patch("src.rag.indexer.build_rag_documents", return_value=current):
            result = update_rag_index_incrementally([_review()], provider, initial_store)
        self.assertTrue(result.initial_build)
        self.assertEqual(provider.texts, ["initial"])
        self.assertEqual(initial_store.metadata["embedding_model"], "text-embedding-3-small")

        with patch("src.rag.indexer.build_rag_documents", return_value=[_document("a", "one"), _document("a", "two")]):
            with self.assertRaisesRegex(ValueError, "Duplicate current RAG document id"):
                update_rag_index_incrementally([_review()], provider, initial_store)

    def test_incremental_requires_full_rebuild_for_incompatible_embedding_model(self) -> None:
        store = MemoryIncrementalStore([_document("a", "same")], {"embedding_model": "other-model"})
        with patch("src.rag.indexer.build_rag_documents", return_value=[_document("a", "same")]):
            with self.assertRaisesRegex(RagIndexRebuildRequiredError, "python -m src.rag.indexer"):
                update_rag_index_incrementally([_review()], FakeEmbeddingProvider(), store)

    def test_incremental_requires_full_rebuild_for_incompatible_schema(self) -> None:
        store = MemoryIncrementalStore([_document("a", "same")], {"rag_document_schema_version": "other"})
        with patch("src.rag.indexer.build_rag_documents", return_value=[_document("a", "same")]):
            with self.assertRaisesRegex(RagIndexRebuildRequiredError, "rag_document_schema_version"):
                update_rag_index_incrementally([_review()], FakeEmbeddingProvider(), store)

    def test_incremental_update_with_temporary_chroma_store_updates_only_changed_and_added(self) -> None:
        initial = [_document("a", "same"), _document("b", "old"), _document("c", "remove")]
        current = [_document("a", "same"), _document("b", "new"), _document("d", "add")]
        with tempfile.TemporaryDirectory() as directory:
            store = ChromaStore(path=Path(directory), collection_name="temporary_incremental_index")
            store.rebuild(
                initial,
                [[0.0], [1.0], [2.0]],
                {"embedding_model": "text-embedding-3-small", "rag_document_schema_version": "1"},
            )
            provider = FakeEmbeddingProvider()
            with patch("src.rag.indexer.build_rag_documents", return_value=current):
                result = update_rag_index_incrementally([_review()], provider, store)

            self.assertEqual(provider.texts, ["add", "new"])
            self.assertEqual({document.id for document in store.get_all_documents()}, {"a", "b", "d"})
            self.assertEqual(result.stored_count, 3)
