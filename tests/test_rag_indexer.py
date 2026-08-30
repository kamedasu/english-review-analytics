from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from src.models import PhraseCard, Review
from src.rag.chroma_store import ChromaStore
from src.rag.embeddings import OpenAIEmbeddingProvider
from src.rag.indexer import rebuild_rag_index
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


class FakeCollection:
    def __init__(self) -> None:
        self.added: dict = {}

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

    def create_collection(self, name: str, embedding_function=None) -> FakeCollection:
        self.created_name = name
        return self.collection


def _review() -> Review:
    return Review(
        review_id="review-1", date=date(2026, 1, 2), topic="test",
        good_points=["A good point"], weak_points=["A weak point"],
        phrase_cards=[PhraseCard(phrase="catch up", meaning="talk")],
        raw_markdown="Entire raw review must not be indexed",
    )


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
